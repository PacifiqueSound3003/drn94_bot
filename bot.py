import json
import logging
import os
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from telegram import ChatPermissions, Update
from telegram.constants import ChatMemberStatus
from telegram.error import Forbidden, BadRequest
from telegram.ext import (
    Application,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
GROUP_ID = int(os.getenv("GROUP_ID", "0"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

DATA_FILE = Path("bot_data.json")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

RULES_TEXT = (
    "📜 *RÈGLES DU GROUPE*\n\n"
    "1. Respectez tous les membres et les administrateurs.\n"
    "2. Les insultes, propos discriminatoires ou comportements irrespectueux sont interdits.\n"
    "3. Aucun contenu illégal ou inapproprié ne sera toléré.\n"
    "4. La publicité et le spam sont strictement interdits.\n"
    "5. Ne partagez pas d'informations personnelles ou confidentielles.\n"
    "6. Les décisions des administrateurs sont définitives.\n"
    "7. Tout manquement aux règles entraînera une sanction immédiate.\n"
    "8. Interdit de ramener des gens en privé pour demander des médias. Tout échange doit se faire ici.\n"
)

AI_WARNING_TEXT = (
    "⚠️ *Analyse du contenu via l’intelligence artificielle.*\n\n"
    "Tout contenu posté pour attirer les membres en DM sera détecté "
    "et l’utilisateur supprimé."
)

DEFAULT_BAD_WORDS = [
    "onlyfans",
    "bitcoin",
    "crypto",
    "dm me",
    "mp moi",
    "viens en privé",
    "écris moi en privé",
    "contacte moi en privé",
    "t.me/",
    "@",
]

LINK_REGEX = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+|discord\.gg/\S+)",
    re.IGNORECASE,
)

SPAM_PATTERNS = [
    re.compile(r"(?i)\b(dm me|mp me|private me|viens en privé|en dm|en mp)\b"),
    re.compile(r"(?i)\b(onlyfans|fansly|crypto|airdrop|giveaway|signal)\b"),
]

TEMP_RESTRICT_MINUTES = 30
AUTO_DELETE_WARNING_AFTER_SECONDS = 3600  # 1h à défaut des "100 vues"


def load_data() -> dict[str, Any]:
    if not DATA_FILE.exists():
        return {
            "join_counter": 0,
            "bad_words": DEFAULT_BAD_WORDS[:],
            "user_strikes": {},
            "last_warning_message_id": None,
        }
    try:
        with DATA_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {
            "join_counter": 0,
            "bad_words": DEFAULT_BAD_WORDS[:],
            "user_strikes": {},
            "last_warning_message_id": None,
        }


def save_data(data: dict[str, Any]) -> None:
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_admin_user(user_id: int) -> bool:
    return user_id == ADMIN_ID


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin_user(user.id):
        if update.effective_message:
            await update.effective_message.reply_text("⛔ Commande réservée à l’admin principal.")
        return False
    return True


def get_strikes(data: dict[str, Any], user_id: int) -> int:
    return int(data["user_strikes"].get(str(user_id), 0))


def add_strike(data: dict[str, Any], user_id: int) -> int:
    new_value = get_strikes(data, user_id) + 1
    data["user_strikes"][str(user_id)] = new_value
    save_data(data)
    return new_value


def reset_strikes(data: dict[str, Any], user_id: int) -> None:
    data["user_strikes"].pop(str(user_id), None)
    save_data(data)


async def restrict_or_ban(
    chat_id: int,
    user_id: int,
    context: ContextTypes.DEFAULT_TYPE,
    reason: str,
) -> None:
    data = context.bot_data["store"]
    strikes = add_strike(data, user_id)

    if strikes >= 3:
        try:
            await context.bot.ban_chat_member(chat_id=chat_id, user_id=user_id)
            await context.bot.send_message(
                chat_id=chat_id,
                text=f"🚫 Utilisateur `{user_id}` banni après 3 restrictions.\nMotif: {reason}",
                parse_mode="Markdown",
            )
        except Exception as e:
            logger.exception("Erreur ban: %s", e)
        return

    try:
        until_date = None
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

        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
            until_date=until_date,
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"⛔ Utilisateur `{user_id}` restreint temporairement ({TEMP_RESTRICT_MINUTES} min).\n"
                f"Motif: {reason}\n"
                f"Avertissements: {strikes}/3"
            ),
            parse_mode="Markdown",
        )

        context.job_queue.run_once(
            unrestrict_user_job,
            when=timedelta(minutes=TEMP_RESTRICT_MINUTES),
            data={"chat_id": chat_id, "user_id": user_id},
            name=f"unrestrict_{chat_id}_{user_id}",
        )

    except Exception as e:
        logger.exception("Erreur restriction: %s", e)


async def unrestrict_user_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    chat_id = job_data["chat_id"]
    user_id = job_data["user_id"]

    try:
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
        await context.bot.restrict_chat_member(
            chat_id=chat_id,
            user_id=user_id,
            permissions=permissions,
        )
    except Exception as e:
        logger.exception("Erreur unrestrict: %s", e)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(
        "Bot de modération actif.\n"
        "Commandes admin:\n"
        "/send <message>\n"
        "/addword <mot>\n"
        "/delword <mot>\n"
        "/listwords\n"
        "/rulesnow\n"
        "/resetstrikes <user_id>\n"
        "/warnnow"
    )


async def send_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    text = " ".join(context.args).strip()
    if not text:
        await update.effective_message.reply_text("Usage: /send votre message")
        return

    await context.bot.send_message(chat_id=GROUP_ID, text=text)
    await update.effective_message.reply_text("✅ Message envoyé au groupe.")


async def addword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    word = " ".join(context.args).strip().lower()
    if not word:
        await update.effective_message.reply_text("Usage: /addword mot_ou_expression")
        return

    data = context.bot_data["store"]
    if word in data["bad_words"]:
        await update.effective_message.reply_text("Déjà dans la liste.")
        return

    data["bad_words"].append(word)
    save_data(data)
    await update.effective_message.reply_text(f"✅ Ajouté: {word}")


async def delword_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    word = " ".join(context.args).strip().lower()
    if not word:
        await update.effective_message.reply_text("Usage: /delword mot_ou_expression")
        return

    data = context.bot_data["store"]
    if word not in data["bad_words"]:
        await update.effective_message.reply_text("Mot introuvable.")
        return

    data["bad_words"].remove(word)
    save_data(data)
    await update.effective_message.reply_text(f"✅ Supprimé: {word}")


async def listwords_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    data = context.bot_data["store"]
    words = data["bad_words"]

    if not words:
        await update.effective_message.reply_text("Liste vide.")
        return

    text = "🚫 Mots interdits:\n\n" + "\n".join(f"- {w}" for w in words[:200])
    await update.effective_message.reply_text(text)


async def resetstrikes_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    if not context.args:
        await update.effective_message.reply_text("Usage: /resetstrikes <user_id>")
        return

    try:
        user_id = int(context.args[0])
    except ValueError:
        await update.effective_message.reply_text("user_id invalide.")
        return

    data = context.bot_data["store"]
    reset_strikes(data, user_id)
    await update.effective_message.reply_text(f"✅ Strikes reset pour {user_id}.")


async def rulesnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    await context.bot.send_message(chat_id=GROUP_ID, text=RULES_TEXT, parse_mode="Markdown")


async def warnnow_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    await send_ai_warning(context)
    await update.effective_message.reply_text("✅ Alerte envoyée.")


async def send_ai_warning(context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        msg = await context.bot.send_message(
            chat_id=GROUP_ID,
            text=AI_WARNING_TEXT,
            parse_mode="Markdown",
        )

        data = context.bot_data["store"]
        data["last_warning_message_id"] = msg.message_id
        save_data(data)

        context.job_queue.run_once(
            delete_warning_job,
            when=timedelta(seconds=AUTO_DELETE_WARNING_AFTER_SECONDS),
            data={"chat_id": GROUP_ID, "message_id": msg.message_id},
            name=f"delete_warning_{msg.message_id}",
        )
    except Exception as e:
        logger.exception("Erreur envoi warning: %s", e)


async def delete_warning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    job_data = context.job.data
    try:
        await context.bot.delete_message(
            chat_id=job_data["chat_id"],
            message_id=job_data["message_id"],
        )
    except Exception:
        pass


async def periodic_warning_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    await send_ai_warning(context)


async def handle_new_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_member_update = update.chat_member
    if not chat_member_update:
        return

    old_status = chat_member_update.old_chat_member.status
    new_status = chat_member_update.new_chat_member.status

    joined = old_status in [ChatMemberStatus.LEFT, ChatMemberStatus.BANNED] and new_status in [
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.RESTRICTED,
        ChatMemberStatus.ADMINISTRATOR,
    ]

    if not joined:
        return

    if update.effective_chat.id != GROUP_ID:
        return

    data = context.bot_data["store"]
    data["join_counter"] += 1
    join_counter = data["join_counter"]
    save_data(data)

    if join_counter >= 100:
        data["join_counter"] = 0
        save_data(data)
        await context.bot.send_message(
            chat_id=GROUP_ID,
            text=RULES_TEXT,
            parse_mode="Markdown",
        )

    # Tentative de suppression d'un éventuel message/service associé à l'arrivée
    # Pas garanti selon les cas/permissions Telegram.
    try:
        if update.effective_message:
            await update.effective_message.delete()
    except Exception:
        pass


async def moderate_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user

    if not message or not chat or not user:
        return

    if chat.id != GROUP_ID:
        return

    # ignorer admin principal
    if is_admin_user(user.id):
        return

    text = message.text or message.caption or ""
    lowered = text.lower()

    # Bloquer liens immédiatement
    if LINK_REGEX.search(lowered):
        try:
            await message.delete()
        except Exception:
            pass
        await restrict_or_ban(chat.id, user.id, context, "Lien interdit")
        return

    # Bloquer mots interdits
    data = context.bot_data["store"]
    for bad_word in data["bad_words"]:
        if bad_word and bad_word.lower() in lowered:
            try:
                await message.delete()
            except Exception:
                pass
            await restrict_or_ban(chat.id, user.id, context, f"Mot interdit: {bad_word}")
            return

    # Spam simple
    for pattern in SPAM_PATTERNS:
        if pattern.search(lowered):
            try:
                await message.delete()
            except Exception:
                pass
            await restrict_or_ban(chat.id, user.id, context, "Spam / incitation DM")
            return


async def post_init(application: Application) -> None:
    application.bot_data["store"] = load_data()
    application.job_queue.run_repeating(
        periodic_warning_job,
        interval=timedelta(hours=3),
        first=timedelta(seconds=30),
        name="periodic_ai_warning",
    )
    logger.info("Bot initialisé.")


def main() -> None:
    if not BOT_TOKEN or not GROUP_ID or not ADMIN_ID:
        raise RuntimeError("BOT_TOKEN, GROUP_ID et ADMIN_ID sont obligatoires.")

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("addword", addword_cmd))
    app.add_handler(CommandHandler("delword", delword_cmd))
    app.add_handler(CommandHandler("listwords", listwords_cmd))
    app.add_handler(CommandHandler("resetstrikes", resetstrikes_cmd))
    app.add_handler(CommandHandler("rulesnow", rulesnow_cmd))
    app.add_handler(CommandHandler("warnnow", warnnow_cmd))

    app.add_handler(ChatMemberHandler(handle_new_chat_member, ChatMemberHandler.CHAT_MEMBER))
    app.add_handler(
        MessageHandler(
            filters.TEXT | filters.Caption(True),
            moderate_message,
        )
    )

    logger.info("Bot lancé.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
