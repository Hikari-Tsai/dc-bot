import ssl
import asyncio
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
import aiohttp
import discord
from discord import app_commands
from dotenv import load_dotenv
from openai import OpenAI

BOT_DIR = Path(__file__).resolve().parent
load_dotenv(BOT_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.2").strip() or "gpt-5.2"
TARGET_CHANNEL_ID = int(os.getenv("TARGET_CHANNEL_ID", "0"))
PROMPT_TOPA_DESCRIPTION="跟帕寶聊天"
PROMPT_TOPA_PATH = BOT_DIR / "prompt_topa.txt"
ASK_LOG_DB_PATH = BOT_DIR / "ask_logs.db"
DEFAULT_PROMPT = "你是一個友善、專業的 Discord AI 助手，請使用繁體中文回答。"


def getenv_int(name: str, default: int, minimum: int = 0) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default

    try:
        parsed_value = int(value)
    except ValueError:
        print(f"[config] invalid {name}={value!r}; using default {default}")
        return default

    if parsed_value < minimum:
        print(
            f"[config] {name} must be >= {minimum}; "
            f"using default {default}"
        )
        return default

    return parsed_value


USER_CONTEXT_LIMIT = getenv_int("USER_CONTEXT_LIMIT", 5, minimum=0)
OWNER_CONTEXT_LIMIT = getenv_int("OWNER_CONTEXT_LIMIT", USER_CONTEXT_LIMIT, minimum=0)
CONTEXT_QUESTION_LIMIT = getenv_int("CONTEXT_QUESTION_LIMIT", 500, minimum=1)
CONTEXT_ANSWER_LIMIT = getenv_int("CONTEXT_ANSWER_LIMIT", 800, minimum=1)
OWNER_PROMPT_HINT = os.getenv(
    "OWNER_PROMPT_HINT",
    "目前發問者是台主。你可以用更熟悉、自然的語氣回應，但仍保持尊重與清楚。",
).strip()


def normalize_name(value: str) -> str:
    return value.strip().casefold()


def getenv_name_set(name: str) -> set[str]:
    raw_value = os.getenv(name, "")
    return {
        normalized
        for item in raw_value.split(",")
        if (normalized := normalize_name(item))
    }


def getenv_int_set(name: str) -> set[int]:
    raw_value = os.getenv(name, "")
    values = set()

    for item in raw_value.split(","):
        item = item.strip()
        if not item:
            continue

        try:
            values.add(int(item))
        except ValueError:
            print(f"[config] ignoring invalid {name} item={item!r}")

    return values


OWNER_USER_IDS = getenv_int_set("OWNER_USER_IDS")
OWNER_USER_NAMES = getenv_name_set("OWNER_USER_NAMES")


def get_user_names(user: discord.abc.User) -> list[str]:
    names = [
        getattr(user, "display_name", ""),
        getattr(user, "global_name", ""),
        getattr(user, "name", ""),
        str(user),
    ]

    unique_names = []
    seen = set()
    for name in names:
        name = str(name).strip()
        normalized = normalize_name(name)
        if name and normalized not in seen:
            unique_names.append(name)
            seen.add(normalized)

    return unique_names


def identify_user(user: discord.abc.User) -> tuple[bool, str, list[str]]:
    user_id = int(user.id)
    user_names = get_user_names(user)

    if user_id in OWNER_USER_IDS:
        return True, "discord_id", user_names

    configured_names = {normalize_name(name) for name in user_names}
    if OWNER_USER_NAMES and configured_names & OWNER_USER_NAMES:
        return True, "user_name", user_names

    return False, "none", user_names


def build_llm_instructions(base_prompt: str, is_owner: bool) -> str:
    if not is_owner or not OWNER_PROMPT_HINT:
        return base_prompt

    return f"{base_prompt}\n\n{OWNER_PROMPT_HINT}"


def load_prompt() -> str:
    try:
        prompt = PROMPT_TOPA_PATH.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return DEFAULT_PROMPT

    return prompt or DEFAULT_PROMPT


def init_ask_log_db() -> None:
    with sqlite3.connect(ASK_LOG_DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS ask_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                interaction_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                user_name TEXT NOT NULL,
                guild_id INTEGER,
                channel_id INTEGER,
                question TEXT NOT NULL,
                answer_text TEXT,
                answered_at TEXT,
                error_text TEXT
            )
        """)
        existing_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(ask_questions)")
        }
        if "answer_text" not in existing_columns:
            conn.execute("ALTER TABLE ask_questions ADD COLUMN answer_text TEXT")
        if "answered_at" not in existing_columns:
            conn.execute("ALTER TABLE ask_questions ADD COLUMN answered_at TEXT")
        if "error_text" not in existing_columns:
            conn.execute("ALTER TABLE ask_questions ADD COLUMN error_text TEXT")


def log_ask_question(
    interaction_id: int,
    user_id: int,
    user_name: str,
    guild_id: int | None,
    channel_id: int | None,
    question: str,
) -> int:
    created_at = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(ASK_LOG_DB_PATH) as conn:
        cursor = conn.execute(
            """
            INSERT INTO ask_questions (
                created_at,
                interaction_id,
                user_id,
                user_name,
                guild_id,
                channel_id,
                question
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                created_at,
                interaction_id,
                user_id,
                user_name,
                guild_id,
                channel_id,
                question,
            ),
        )
        log_id = int(cursor.lastrowid)

    print(
        "[db] inserted ask question "
        f"id={log_id} user_id={user_id} guild_id={guild_id} "
        f"channel_id={channel_id} question={question!r}"
    )
    return log_id


def update_ask_answer(log_id: int, answer: str) -> None:
    answered_at = datetime.now(timezone.utc).isoformat()

    with sqlite3.connect(ASK_LOG_DB_PATH) as conn:
        conn.execute(
            """
            UPDATE ask_questions
            SET answer_text = ?, answered_at = ?, error_text = NULL
            WHERE id = ?
            """,
            (answer, answered_at, log_id),
        )

    print(f"[db] updated ask answer id={log_id} answer={answer!r}")


def update_ask_error(log_id: int, error_text: str) -> None:
    with sqlite3.connect(ASK_LOG_DB_PATH) as conn:
        conn.execute(
            "UPDATE ask_questions SET error_text = ? WHERE id = ?",
            (error_text, log_id),
        )

    print(f"[db] updated ask error id={log_id} error={error_text!r}")


def fetch_recent_user_context(
    user_id: int,
    limit: int = USER_CONTEXT_LIMIT,
) -> list[tuple[str, str]]:
    user_id = int(user_id)
    limit = int(limit)
    if limit <= 0:
        print(f"[db] skipped user context user_id={user_id} limit={limit}")
        return []

    with sqlite3.connect(ASK_LOG_DB_PATH) as conn:
        rows = conn.execute(
            """
            SELECT question, answer_text
            FROM ask_questions
            WHERE user_id = ?
              AND answer_text IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()

    history = [(question, answer) for question, answer in reversed(rows)]
    print(
        "[db] fetched user context "
        f"exact_user_id={user_id} limit={limit} count={len(history)}"
    )
    for index, (question, answer) in enumerate(history, start=1):
        print(
            "[db] context "
            f"{index}/{len(history)} user_id={user_id} "
            f"question={question!r} answer={answer!r}"
        )

    return history


def compact_text(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text

    return text[:limit].rstrip() + "...（已截斷）"


def build_llm_input(
    question: str,
    history: list[tuple[str, str]],
    user_names: list[str],
    is_owner: bool,
    owner_match_source: str,
) -> str:
    owner_status = "是台主（已由系統確認）" if owner_match_source == "discord_id" else "一般使用者"
    if is_owner and owner_match_source == "user_name":
        owner_status = "可能是台主（由使用者名稱推測，可靠度低於系統確認）"

    parts = [
        "以下是本次 Discord 發問者資訊，請用來辨識對方是誰。",
        "如果使用者詢問自己是誰、你認不認得他、是否為台主，請依此資訊回答。",
        "不要在回覆中透露 Discord user ID 或其他內部識別資訊。",
        "",
        "【本次發問者】",
        f"可見名稱：{', '.join(user_names) if user_names else '未知'}",
        f"台主狀態：{owner_status}",
    ]

    if history:
        parts.extend([
            "",
            "【同一位使用者最近的歷史對話】",
            "請只在有幫助時參考，不要主動提及你正在讀取紀錄。",
        ])

        for index, (past_question, past_answer) in enumerate(history, start=1):
            parts.extend([
                f"{index}. 使用者：{compact_text(past_question, CONTEXT_QUESTION_LIMIT)}",
                f"   助手：{compact_text(past_answer, CONTEXT_ANSWER_LIMIT)}",
            ])

    parts.extend([
        "",
        "【這次的問題】",
        question,
    ])

    return "\n".join(parts)

llm = OpenAI(api_key=OPENAI_API_KEY)
prompt_topa = load_prompt()
init_ask_log_db()


class LlmBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True

        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def _async_setup_hook(self):
        await super()._async_setup_hook()

        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        self.http.connector = aiohttp.TCPConnector(ssl=ssl_context, limit=0)

    async def setup_hook(self):
        await self.tree.sync()


bot = LlmBot()


@bot.event
async def on_ready():
    print(f"Bot 已登入：{bot.user}")


@bot.tree.command(name="ask", description=PROMPT_TOPA_DESCRIPTION)
@app_commands.describe(question="你想問的問題")
async def ask(interaction: discord.Interaction, question: str):
    try:
        await interaction.response.defer(thinking=True)
    except discord.NotFound:
        print("Interaction 已過期或已被其他 bot 程序回應，略過本次 ask。")
        return

    #if interaction.channel_id != TARGET_CHANNEL_ID:
    #    await interaction.followup.send(
    #        "這個指令只能在指定頻道使用。",
    #        ephemeral=True
    #    )
    #    return

    is_owner, owner_match_source, user_names = identify_user(interaction.user)
    context_limit = OWNER_CONTEXT_LIMIT if is_owner else USER_CONTEXT_LIMIT
    print(
        "[identity] "
        f"user_id={interaction.user.id} names={user_names!r} "
        f"is_owner={is_owner} match_source={owner_match_source}"
    )

    try:
        history = await asyncio.to_thread(
            fetch_recent_user_context,
            interaction.user.id,
            context_limit,
        )
    except Exception as exc:
        print(f"Failed to fetch user context: {exc}")
        history = []

    log_id = None
    try:
        log_id = await asyncio.to_thread(
            log_ask_question,
            interaction.id,
            interaction.user.id,
            str(interaction.user),
            interaction.guild_id,
            interaction.channel_id,
            question,
        )
    except Exception as exc:
        print(f"Failed to log ask question: {exc}")

    try:
        response = await asyncio.to_thread(
            llm.responses.create,
            model=OPENAI_MODEL,
            instructions=build_llm_instructions(prompt_topa, is_owner),
            input=build_llm_input(
                question,
                history,
                user_names,
                is_owner,
                owner_match_source,
            ),
        )
    except Exception as exc:
        print(f"LLM request failed: {exc}")
        if log_id is not None:
            try:
                await asyncio.to_thread(update_ask_error, log_id, str(exc))
            except Exception as log_exc:
                print(f"Failed to log ask error: {log_exc}")
        await interaction.followup.send(
            "處理問題時發生錯誤，請稍後再試。",
            ephemeral=True
        )
        return

    answer = response.output_text

    if len(answer) > 1900:
        answer = answer[:1900] + "\n\n...內容過長，已截斷"

    if log_id is not None:
        try:
            await asyncio.to_thread(update_ask_answer, log_id, response.output_text)
        except Exception as exc:
            print(f"Failed to log ask answer: {exc}")

    await interaction.followup.send(answer)


bot.run(DISCORD_TOKEN)
