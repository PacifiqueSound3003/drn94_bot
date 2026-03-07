import logging
import os
import re
from datetime import datetime, timedelta, timezone
from html import escape
from typing import Optional

import psycopg
from dotenv import load_dotenv
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, Forbidden
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_IDS = [int(x) for x in os.getenv("ADMIN_ID", "").split(",") if x]
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN manquant.")
if not ADMIN_IDS:
    raise RuntimeError("ADMIN_ID manquant.")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL manquant.")

RULES_TEXT = (
    "📜 <b>RÈGLES DU GROUPE</b>\n\n"
    "1. Respectez tous les membres et les administrateurs.\n"
    "2. Les insultes, propos discriminatoires ou comportements irrespectueux sont interdits.\n"
    "3. Aucun contenu illégal ou inapproprié ne sera toléré.\n"
    "4. La publicité et le spam sont strictement interdits.\n"
    "5. Ne partagez pas d'informations personnelles ou confidentielles.\n"
    "6. Les décisions des administrateurs sont définitives.\n"
    "7. Tout manquement aux règles entraînera une sanction immédiate.\n"
    "8. Interdit de ramener des gens en privé pour demander des médias. Tout échange doit se faire ici.\n"
    "9. Les tentatives d’attirer des membres en DM sont interdites."
)

AI_WARNING_TEXT = (
    "🚨⚠️ <b>Analyse du contenu via l’intelligence artificielle.</b>\n\n"
    "Tout contenu posté pour attirer les membres en DM sera détecté pour notre IA "
    "et l’utilisateur supprimé - blacklisté. 🚨🚨"
)

DEFAULT_BAD_WORDS = [
    "onlyfans",
    "fansly",
    "viens en privé",
    "viens pv",
    "viens en dm",
    "dm moi",
    "mp moi",
    "écris moi en privé",
    "contacte moi en privé",
    "ajoute moi sur snap",
    "snap moi",
]

TEMP_RESTRICT_MINUTES = 30
BAN_AFTER_STRIKES = 3
RULES_EVERY_JOINS = 100
WARNING_EVERY_HOURS = 3
DELETE_WARNING_AFTER_MINUTES = 60

LINK_REGEX = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|discord\.gg/|discord\.com/invite/|bit\.ly/|tinyurl\.com/)\S+"
)

USERNAME_BAIT_REGEX = re.compile(
    r"(?i)(?:dm|mp|priv[eé]|pv|private|contacte|écris|viens).{0,25}(?:moi|me)"
)

SPAM_PATTERNS = [
    re.compile(r"(?i)\b(?:onlyfans|fansly|crypto|airdrop|signal|escort)\b"),
    re.compile(r"(?i)\b(?:dm me|mp me|private me|come dm|message me privately)\b"),
]

ADMIN_STATE: dict[int, dict] = {}

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# =========================================================
# DB
# =========================================================


def get_conn():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS groups (
                    group_id BIGINT PRIMARY KEY,
                    title TEXT,
                    is_active BOOLEAN NOT NULL DEFAULT TRUE,
                    join_counter INTEGER NOT NULL DEFAULT 0,
                    last_warning_message_id BIGINT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS bad_words (
                    id BIGSERIAL PRIMARY KEY,
                    group_id BIGINT NOT NULL,
                    word TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    UNIQUE(group_id, word)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS user_strikes (
                    group_id BIGINT NOT NULL,
                    user_id BIGINT NOT NULL,
                    strikes INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP NOT NULL DEFAULT NOW(),
                    PRIMARY KEY (group_id, user_id)
                );
                """
            )

            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS moderation_logs (
                    id BIGSERIAL PRIMARY KEY,
                    group_id BIGINT NOT NULL,
                    user_id BIGINT,
                    action TEXT NOT NULL,
                    reason TEXT,
                    created_at TIMESTAMP NOT NULL DEFAULT NOW()
                );
                """
            )

            conn.commit()


# =========================================================
# DB HELPERS
# =========================================================


def is_main_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def upsert_group(chat_id: int, title: str, is_active: bool = True):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO groups (group_id, title, is_active, updated_at)
                VALUES (%s, %s, %s, NOW())
                ON CONFLICT (group_id)
                DO UPDATE SET title = EXCLUDED.title, is_active = EXCLUDED.is_active, updated_at = NOW();
                """,
                (chat_id, title, is_active),
            )
            conn.commit()


def set_group_active(chat_id: int, is_active: bool):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE groups
                SET is_active = %s, updated_at = NOW()
                WHERE group_id = %s;
                """,
                (is_active, chat_id),
            )
            conn.commit()


def get_group(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT group_id, title, is_active, join_counter, last_warning_message_id
                FROM groups
                WHERE group_id = %s;
                """,
                (chat_id,),
            )
            return cur.fetchone()


def get_all_active_groups():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT group_id, COALESCE(title, CAST(group_id AS TEXT))
                FROM groups
                WHERE is_active = TRUE
                ORDER BY created_at ASC;
                """
            )
            return cur.fetchall()


def is_group_active(chat_id: int) -> bool:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT is_active FROM groups WHERE group_id = %s;",
                (chat_id,),
            )
            row = cur.fetchone()
            return bool(row[0]) if row else True


def increment_join_counter(chat_id: int, title: str) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO groups (group_id, title, join_counter, is_active, updated_at)
                VALUES (%s, %s, 1, TRUE, NOW())
                ON CONFLICT (group_id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    join_counter = groups.join_counter + 1,
                    is_active = TRUE,
                    updated_at = NOW()
                RETURNING join_counter;
                """,
                (chat_id, title),
            )
            value = cur.fetchone()[0]
            conn.commit()
            return value


def reset_join_counter(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE groups
                SET join_counter = 0, updated_at = NOW()
                WHERE group_id = %s;
                """,
                (chat_id,),
            )
            conn.commit()


def set_last_warning_message_id(chat_id: int, message_id: Optional[int]):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE groups
                SET last_warning_message_id = %s, updated_at = NOW()
                WHERE group_id = %s;
                """,
                (message_id, chat_id),
            )
            conn.commit()


def ensure_default_bad_words(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            for word in DEFAULT_BAD_WORDS:
                cur.execute(
                    """
                    INSERT INTO bad_words (group_id, word)
                    VALUES (%s, %s)
                    ON CONFLICT (group_id, word) DO NOTHING;
                    """,
                    (chat_id, word.lower()),
                )
            conn.commit()


def add_bad_word(chat_id: int, word: str) -> bool:
    word = word.strip().lower()
    if not word:
        return False

    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO bad_words (group_id, word)
                VALUES (%s, %s)
                ON CONFLICT (group_id, word) DO NOTHING
                RETURNING id;
                """,
                (chat_id, word),
            )
            row = cur.fetchone()
            conn.commit()
            return row is not None


def remove_bad_word(chat_id: int, word: str) -> bool:
    word = word.strip().lower()
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM bad_words
                WHERE group_id = %s AND word = %s
                RETURNING id;
                """,
                (chat_id, word),
            )
            row = cur.fetchone()
            conn.commit()
            return row is not None


def list_bad_words(chat_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT word
                FROM bad_words
                WHERE group_id = %s
                ORDER BY word ASC;
                """,
                (chat_id,),
            )
            return [row[0] for row in cur.fetchall()]


def get_strikes(chat_id: int, user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT strikes
                FROM user_strikes
                WHERE group_id = %s AND user_id = %s;
                """,
                (chat_id, user_id),
            )
            row = cur.fetchone()
            return int(row[0]) if row else 0


def add_strike(chat_id: int, user_id: int) -> int:
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO user_strikes (group_id, user_id, strikes, updated_at)
                VALUES (%s, %s, 1, NOW())
                ON CONFLICT (group_id, user_id)
                DO UPDATE SET strikes = user_strikes.strikes + 1, updated_at = NOW()
                RETURNING strikes;
                """,
                (chat_id, user_id),
            )
            strikes = int(cur.fetchone()[0])
            conn.commit()
            return strikes


def reset_strikes(chat_id: int, user_id: int):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM user_strikes
                WHERE group_id = %s AND user_id = %s;
                """,
                (chat_id, user_id),
            )
            conn.commit()


def log_action(chat_id: int, user_id: Optional[int], action: str, reason: Optional[str] = None):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO moderation_logs (group_id, user_id, action, reason)
                VALUES (%s, %s, %s, %s);
                """,
                (chat_id, user_id, action, reason),
            )
            conn.commit()


# =========================================================
# KEYBOARDS
# =========================================================


def admin_main_keyboard():
    keyboard = [
        [InlineKeyboardButton("👥 Choisir un groupe", callback_data="admin_groups")],
        [InlineKeyboardButton("📢 Envoyer un message", callback_data="admin_send_pick_group")],
        [InlineKeyboardButton("🚫 Mots interdits", callback_data="admin_words_pick_group")],
    ]
    return InlineKeyboardMarkup(keyboard)


def admin_back_keyboard():
    keyboard = [
        [InlineKeyboardButton("⬅️ Menu principal", callback_data="admin_home")]
    ]
    return InlineKeyboardMarkup(keyboard)


def group_list_keyboard(groups, prefix: str):
    keyboard = []
    for group_id, title in groups:
        label = f"👥 {str(title)[:40]}"
        keyboard.append(
            [InlineKeyboardButton(label, callback_data=f"{prefix}:{group_id}")]
        )

    keyboard.append(
        [InlineKeyboardButton("⬅️ Menu principal", callback_data="admin_home")]
    )
    return InlineKeyboardMarkup(keyboard)


def group_actions_keyboard(group_id: int, is_active: bool = True):
    toggle_label = "🟢 Désactiver modération" if is_active else "⚪ Activer modération"
    keyboard = [
        [InlineKeyboardButton("📢 Envoyer un message", callback_data=f"group_send:{group_id}")],
        [InlineKeyboardButton("📜 Envoyer les règles", callback_data=f"group_rules:{group_id}")],
        [InlineKeyboardButton("🚫 Gérer les mots interdits", callback_data=f"group_words:{group_id}")],
        [InlineKeyboardButton(toggle_label, callback_data=f"group_toggle:{group_id}")],
        [InlineKeyboardButton("⬅️ Retour aux groupes", callback_data="admin_groups")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="admin_home")],
    ]
    return InlineKeyboardMarkup(keyboard)


def group_words_keyboard(group_id: int):
    keyboard = [
        [InlineKeyboardButton("➕ Ajouter un mot", callback_data=f"words_add:{group_id}")],
        [InlineKeyboardButton("➖ Supprimer un mot", callback_data=f"words_remove:{group_id}")],
        [InlineKeyboardButton("⬅️ Retour au groupe", callback_data=f"group_menu:{group_id}")],
        [InlineKeyboardButton("🏠 Menu principal", callback_data="admin_home")],
    ]
    return InlineKeyboardMarkup(keyboard)


# =========================================================
# HELPERS
# =========================================================


async def ensure_admin_user(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_main_admin(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Accès refusé.")
        return False
    return True


def clear_admin_state(user_id: int):
    ADMIN_STATE.pop(user_id, None)


def set_admin_state(user_id: int, action: str, group_id: Optional[int] = None):
    ADMIN_STATE[user_id] = {
        "action": action,
        "group_id": group_id,
    }


async def delete_message_safe(message):
    try:
        await message.delete()
    except Exception:
        pass


async def user_is_group_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    try:
        member = await context.bot.get_chat_member(chat_id, user_id)
        return member.status in [ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER]
    except Exception:
        return False


def text_contains_link(message_text: str) -> bool:
    return bool(LINK_REGEX.search(message_text))


async def restrict_or_ban(
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str,
):
    strikes = add_strike(chat_id, user_id)
    log_action(chat_id, user_id, "strike", reason)

    if strikes >= BAN_AFTER_STRIKES:
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🚫 Utilisateur <code>{user_id}</code> banni après "
                    f"{BAN_AFTER_STRIKES} sanctions.\nMotif : {escape(reason)}"
                ),
                parse_mode=ParseMode.HTML,
            )
            log_action(chat_id, user_id, "ban", reason)
        except Exception as e:
            logger.exception("Erreur ban user=%s chat=%s : %s", user_id, chat_id, e)
        return

    until_date = datetime.now(timezone.utc) + timedelta(minutes=TEMP_RESTRICT_MINUTES)

    permissions = ChatPermissions(
        can_send_messages=False,
        can_send_audios=False,
        can_send_documents=False,
        can_send_photos=False,
        can_send_videos=False,
        can_send_video_notes=False,
        can_send_voice_notes=False,
        can_send_polls=False,
        can_send_other_messages=False,
        can_add_web_page_previews=False,
        can_change_info=False,
        can_invite_users=False,
        can_pin_messages=False,
        can_manage_topics=False,
    )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            until_date=until_date,
        )
        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⛔ Utilisateur <code>{user_id}</code> restreint pendant "
                f"{TEMP_RESTRICT_MINUTES} min.\n"
                f"Motif : {escape(reason)}\n"
                f"Sanctions : {strikes}/{BAN_AFTER_STRIKES}"
            ),
            parse_mode=ParseMode.HTML,
        )
        log_action(chat_id, user_id, "restrict", reason)

        context.job_queue.run_once(
            unrestrict_user_job,
            when=timedelta(minutes=TEMP_RESTRICT_MINUTES),
            data={"chat_id": chat_id, "user_id": user_id},
            name=f"unrestrict_{chat_id}_{user_id}",
        )
    except Exception as e:
        logger.exception("Erreur restriction user=%s chat=%s : %s", user_id, chat_id, e)


async def unrestrict_user_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    chat_id = data["chat_id"]
    user_id = data["user_id"]

    permissions = ChatPermissions(
        can_send_messages=True,
        can_send_audios=True,
        can_send_documents=True,
        can_send_photos=True,
        can_send_videos=True,
        can_send_video_notes=True,
        can_send_voice_notes=True,
        can_send_polls=True,
        can_send_other_messages=True,
        can_add_web_page_previews=True,
        can_change_info=False,
        can_invite_users=True,
        can_pin_messages=False,
        can_manage_topics=False,
    )

    try:
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
        )
        log_action(chat_id, user_id, "unrestrict", "fin de restriction")
    except Exception as e:
        logger.exception("Erreur unrestrict user=%s chat=%s : %s", user_id, chat_id, e)


async def send_ai_warning_to_group(chat_id: int, context: ContextTypes.DEFAULT_TYPE):
    try:
        msg = await context.bot.send_message(
            chat_id=chat_id,
            text=AI_WARNING_TEXT,
            parse_mode=ParseMode.HTML,
        )
        set_last_warning_message_id(chat_id, msg.message_id)

        context.job_queue.run_once(
            delete_warning_job,
            when=timedelta(minutes=DELETE_WARNING_AFTER_MINUTES),
            data={"chat_id": chat_id, "message_id": msg.message_id},
            name=f"delete_warn_{chat_id}_{msg.message_id}",
        )
        log_action(chat_id, None, "warning_sent", "message dissuasif")
    except Forbidden:
        logger.warning("Bot interdit dans le groupe %s", chat_id)
    except BadRequest as e:
        logger.warning("Impossible d'envoyer le warning dans %s : %s", chat_id, e)
    except Exception as e:
        logger.exception("Erreur send_ai_warning_to_group chat=%s : %s", chat_id, e)


async def delete_warning_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=data["chat_id"],
            message_id=data["message_id"],
        )
        set_last_warning_message_id(data["chat_id"], None)
    except Exception:
        pass


async def show_groups_menu(query, mode: str):
    groups = get_all_active_groups()

    if not groups:
        await query.edit_message_text(
            "Aucun groupe actif trouvé.",
            reply_markup=admin_back_keyboard(),
        )
        return

    await query.edit_message_text(
        text="Choisis un groupe :",
        reply_markup=group_list_keyboard(groups, mode),
    )


async def show_group_menu(query, group_id: int):
    group = get_group(group_id)
    if not group:
        await query.edit_message_text(
            "Groupe introuvable.",
            reply_markup=admin_back_keyboard(),
        )
        return

    _, title, is_active, join_counter, _ = group
    title = title or str(group_id)

    text = (
        f"👥 <b>{escape(title)}</b>\n\n"
        f"ID : <code>{group_id}</code>\n"
        f"Modération : {'active' if is_active else 'inactive'}\n"
        f"Nouveaux membres comptés : {join_counter}/{RULES_EVERY_JOINS}"
    )

    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=group_actions_keyboard(group_id, bool(is_active)),
    )


async def show_group_words_menu(query, group_id: int):
    group = get_group(group_id)
    title = group[1] if group else str(group_id)
    words = list_bad_words(group_id)

    if words:
        preview = "\n".join(f"• {escape(w)}" for w in words[:25])
        if len(words) > 25:
            preview += f"\n… et {len(words) - 25} autre(s)"
    else:
        preview = "Aucun mot interdit."

    text = (
        f"🚫 <b>Mots interdits</b>\n"
        f"👥 {escape(str(title))}\n\n"
        f"{preview}"
    )

    await query.edit_message_text(
        text=text,
        parse_mode=ParseMode.HTML,
        reply_markup=group_words_keyboard(group_id),
    )


# =========================================================
# COMMANDES
# =========================================================


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not user or not chat or not message:
        return

    if not is_main_admin(user.id):
        await message.reply_text("Tu n’es pas admin, garde la pêche 🍑")
        return

    if chat.type != ChatType.PRIVATE:
        return

    clear_admin_state(user.id)

    await message.reply_text(
        text="⚙️ <b>Panel Admin</b>\n\nChoisis une action :",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )


async def panel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not user or not chat or not message:
        return

    if not is_main_admin(user.id):
        await message.reply_text("Tu n’es pas admin, garde la pêche 🍑")
        return

    if chat.type != ChatType.PRIVATE:
        return

    clear_admin_state(user.id)

    await message.reply_text(
        text="⚙️ <b>Panel Admin</b>\n\nChoisis une action :",
        parse_mode=ParseMode.HTML,
        reply_markup=admin_main_keyboard(),
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not is_main_admin(user.id):
        return

    clear_admin_state(user.id)
    await update.effective_message.reply_text("Action annulée.")


async def rulesnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin_user(update):
        return

    chat = update.effective_chat
    if not chat or chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.effective_message.reply_text("Utilise cette commande dans un groupe.")
        return

    upsert_group(chat.id, chat.title or str(chat.id), True)
    ensure_default_bad_words(chat.id)

    await context.bot.send_message(
        chat_id=chat.id,
        text=RULES_TEXT,
        parse_mode=ParseMode.HTML,
    )


async def warnnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await ensure_admin_user(update):
        return

    chat = update.effective_chat
    if not chat or chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        await update.effective_message.reply_text("Utilise cette commande dans un groupe.")
        return

    upsert_group(chat.id, chat.title or str(chat.id), True)
    ensure_default_bad_words(chat.id)

    await send_ai_warning_to_group(chat.id, context)
    await update.effective_message.reply_text("✅ Message envoyé.")


# =========================================================
# CALLBACKS PANEL
# =========================================================


async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user

    if not query or not user:
        return

    await query.answer()

    if not is_main_admin(user.id):
        await query.answer("Accès refusé", show_alert=True)
        return

    data = query.data or ""

    if data == "admin_home":
        clear_admin_state(user.id)
        await query.edit_message_text(
            text="⚙️ <b>Panel Admin</b>\n\nChoisis une action :",
            parse_mode=ParseMode.HTML,
            reply_markup=admin_main_keyboard(),
        )
        return

    if data == "admin_groups":
        await show_groups_menu(query, "group_menu")
        return

    if data == "admin_send_pick_group":
        await show_groups_menu(query, "pick_send")
        return

    if data == "admin_words_pick_group":
        await show_groups_menu(query, "pick_words")
        return

    if data.startswith("group_menu:"):
        group_id = int(data.split(":")[1])
        await show_group_menu(query, group_id)
        return

    if data.startswith("pick_send:"):
        group_id = int(data.split(":")[1])
        set_admin_state(user.id, "send_message", group_id)
        await query.edit_message_text(
            text=(
                "📢 Envoie maintenant le message à diffuser dans ce groupe.\n\n"
                "Le prochain message texte que tu enverras ici sera envoyé au groupe."
            ),
            reply_markup=admin_back_keyboard(),
        )
        return

    if data.startswith("pick_words:"):
        group_id = int(data.split(":")[1])
        await show_group_words_menu(query, group_id)
        return

    if data.startswith("group_send:"):
        group_id = int(data.split(":")[1])
        set_admin_state(user.id, "send_message", group_id)
        await query.edit_message_text(
            text=(
                "📢 Envoie maintenant le message à diffuser dans ce groupe.\n\n"
                "Le prochain message texte que tu enverras ici sera envoyé au groupe."
            ),
            reply_markup=admin_back_keyboard(),
        )
        return

    if data.startswith("group_rules:"):
        group_id = int(data.split(":")[1])
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=RULES_TEXT,
                parse_mode=ParseMode.HTML,
            )
            log_action(group_id, user.id, "manual_rules", "règles envoyées manuellement")
            await query.answer("Règles envoyées.", show_alert=False)
        except Exception as e:
            logger.exception("Erreur envoi règles: %s", e)
            await query.answer("Échec de l'envoi.", show_alert=True)
        await show_group_menu(query, group_id)
        return

    if data.startswith("group_toggle:"):
        group_id = int(data.split(":")[1])
        current = is_group_active(group_id)
        set_group_active(group_id, not current)
        log_action(group_id, user.id, "toggle_group", f"is_active={not current}")
        await show_group_menu(query, group_id)
        return

    if data.startswith("group_words:"):
        group_id = int(data.split(":")[1])
        await show_group_words_menu(query, group_id)
        return

    if data.startswith("words_add:"):
        group_id = int(data.split(":")[1])
        set_admin_state(user.id, "add_word", group_id)
        await query.edit_message_text(
            text="➕ Envoie maintenant le mot ou l’expression à interdire.",
            reply_markup=admin_back_keyboard(),
        )
        return

    if data.startswith("words_remove:"):
        group_id = int(data.split(":")[1])
        set_admin_state(user.id, "remove_word", group_id)
        await query.edit_message_text(
            text="➖ Envoie maintenant le mot ou l’expression à supprimer de la liste.",
            reply_markup=admin_back_keyboard(),
        )
        return


# =========================================================
# GESTION DES MESSAGES PRIVÉS DE L'ADMIN
# =========================================================


async def admin_private_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    chat = update.effective_chat
    message = update.effective_message

    if not user or not chat or not message:
        return

    if chat.type != ChatType.PRIVATE:
        return

    if not is_main_admin(user.id):
        return

    state = ADMIN_STATE.get(user.id)
    if not state:
        return

    text = (message.text or "").strip()
    if not text:
        return

    action = state.get("action")
    group_id = state.get("group_id")

    if not group_id:
        clear_admin_state(user.id)
        await message.reply_text("Action invalide. Recommence avec /panel")
        return

    if action == "send_message":
        try:
            await context.bot.send_message(
                chat_id=group_id,
                text=text,
            )
            log_action(group_id, user.id, "manual_send", "message personnalisé")
            clear_admin_state(user.id)
            await message.reply_text("✅ Message envoyé.", reply_markup=admin_main_keyboard())
        except Exception as e:
            logger.exception("Erreur send_message panel: %s", e)
            await message.reply_text("❌ Échec de l'envoi.")
        return

    if action == "add_word":
        added = add_bad_word(group_id, text.lower())
        clear_admin_state(user.id)
        if added:
            log_action(group_id, user.id, "add_bad_word", text.lower())
            await message.reply_text(
                f"✅ Mot ajouté : {text.lower()}",
                reply_markup=admin_main_keyboard(),
            )
        else:
            await message.reply_text(
                "ℹ️ Ce mot existe déjà ou est invalide.",
                reply_markup=admin_main_keyboard(),
            )
        return

    if action == "remove_word":
        removed = remove_bad_word(group_id, text.lower())
        clear_admin_state(user.id)
        if removed:
            log_action(group_id, user.id, "remove_bad_word", text.lower())
            await message.reply_text(
                f"✅ Mot supprimé : {text.lower()}",
                reply_markup=admin_main_keyboard(),
            )
        else:
            await message.reply_text(
                "ℹ️ Mot introuvable.",
                reply_markup=admin_main_keyboard(),
            )
        return


# =========================================================
# TRACK GROUPS
# =========================================================


async def my_chat_member_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cmu = update.my_chat_member
    chat = update.effective_chat

    if not cmu or not chat:
        return

    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    old_status = cmu.old_chat_member.status
    new_status = cmu.new_chat_member.status

    title = chat.title or str(chat.id)

    if new_status in [ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR]:
        upsert_group(chat.id, title, True)
        ensure_default_bad_words(chat.id)
        logger.info("Bot actif dans le groupe %s (%s)", title, chat.id)

    if new_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED]:
        upsert_group(chat.id, title, False)
        logger.info("Bot retiré du groupe %s (%s)", title, chat.id)

    logger.info("my_chat_member change %s -> %s dans %s", old_status, new_status, chat.id)


# =========================================================
# NOUVEAUX MEMBRES
# =========================================================


async def handle_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat

    if not message or not chat:
        return

    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    title = chat.title or str(chat.id)
    upsert_group(chat.id, title, True)
    ensure_default_bad_words(chat.id)

    # Tenter de supprimer le message système d'arrivée
    await delete_message_safe(message)

    if not is_group_active(chat.id):
        return

    for _member in message.new_chat_members:
        current = increment_join_counter(chat.id, title)
        if current >= RULES_EVERY_JOINS:
            reset_join_counter(chat.id)
            try:
                await context.bot.send_message(
                    chat_id=chat.id,
                    text=RULES_TEXT,
                    parse_mode=ParseMode.HTML,
                )
                log_action(chat.id, None, "rules_auto", "100 nouveaux membres")
            except Exception as e:
                logger.exception("Erreur envoi auto des règles: %s", e)


# =========================================================
# MODÉRATION
# =========================================================


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.type not in [ChatType.GROUP, ChatType.SUPERGROUP]:
        return

    upsert_group(chat.id, chat.title or str(chat.id), True)
    ensure_default_bad_words(chat.id)

    if not is_group_active(chat.id):
        return

    if user.id == ADMIN_ID:
        return

    if await user_is_group_admin(chat.id, user.id, context):
        return

    text = (message.text or message.caption or "").strip()
    lowered = text.lower()

    # Liens
    if text and text_contains_link(lowered):
        await delete_message_safe(message)
        await restrict_or_ban(chat.id, user.id, context, "Lien interdit")
        return

    # DM bait / spam ciblé
    if text and USERNAME_BAIT_REGEX.search(lowered):
        await delete_message_safe(message)
        await restrict_or_ban(chat.id, user.id, context, "Incitation au privé / DM")
        return

    # Mots interdits
    if text:
        bad_words = list_bad_words(chat.id)
        for bad_word in bad_words:
            if bad_word and bad_word in lowered:
                await delete_message_safe(message)
                await restrict_or_ban(chat.id, user.id, context, f"Mot interdit : {bad_word}")
                return

    # Spam générique
    if text:
        for pattern in SPAM_PATTERNS:
            if pattern.search(lowered):
                await delete_message_safe(message)
                await restrict_or_ban(chat.id, user.id, context, "Spam / contenu interdit")
                return


# =========================================================
# JOB PÉRIODIQUE
# =========================================================


async def periodic_warning_job(context: ContextTypes.DEFAULT_TYPE):
    groups = get_all_active_groups()
    for group_id, _title in groups:
        await send_ai_warning_to_group(group_id, context)


# =========================================================
# POST INIT
# =========================================================


async def post_init(application: Application):
    init_db()
    application.job_queue.run_repeating(
        periodic_warning_job,
        interval=timedelta(hours=WARNING_EVERY_HOURS),
        first=timedelta(seconds=30),
        name="periodic_ai_warning",
    )
    logger.info("Bot initialisé.")


# =========================================================
# MAIN
# =========================================================


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("panel", panel_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("rulesnow", rulesnow_cmd))
    app.add_handler(CommandHandler("warnnow", warnnow_cmd))

    app.add_handler(CallbackQueryHandler(admin_callback_handler))
    app.add_handler(ChatMemberHandler(my_chat_member_handler, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, handle_new_members))

    # Messages privés de l'admin pour les actions du panel
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.ChatType.PRIVATE,
            admin_private_text_handler,
        )
    )

    # Modération générale dans les groupes
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.ChatType.PRIVATE,
            moderate_message,
        )
    )

    logger.info("Bot lancé.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
