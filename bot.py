import logging
import os
import json
import datetime
from functools import wraps
from contextlib import contextmanager

from sqlalchemy import create_engine, Column, Integer, String, DateTime, Text, func
from sqlalchemy.orm import declarative_base, sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from telegram import (
    Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    constants
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# --- CONFIGURATION ---
# It's recommended to set this as an environment variable for security.
TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "7999315049:AAEzcFleeLhEXd2_Akf6EnrublN1xuOvYFg") 
ADMIN_CONFIG_FILE = "admin_config.json"
DB_FILE = "feedback_pro.db"
FEEDBACK_CATEGORIES = ["🐛 Bug Report", "💡 Feature Request", "🤔 General Query", "⭐ Other"]
FEEDBACK_PER_PAGE = 5 # Number of feedback entries to show per page for admins

# --- LOGGING SETUP ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- DATABASE SETUP (SQLAlchemy) ---
Base = declarative_base()
engine = create_engine(f'sqlite:///{DB_FILE}')
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

class Feedback(Base):
    """Database model for storing feedback."""
    __tablename__ = 'feedback'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, index=True, nullable=False)
    username = Column(String)
    category = Column(String, nullable=False)
    message = Column(Text, nullable=False)
    rating = Column(Integer)
    status = Column(String, default='new', index=True)  # 'new', 'replied', 'resolved'
    timestamp = Column(DateTime, default=datetime.datetime.utcnow)
    photo_file_id = Column(String, nullable=True)

# Create tables if they don't exist
Base.metadata.create_all(bind=engine)

# --- CONVERSATION STATES ---
# Simplified the conversation flow
(
    SELECT_CATEGORY,
    GET_FEEDBACK_CONTENT,
    GET_RATING,
    CONFIRM_SUBMISSION,
    ADMIN_REPLY,
    ADMIN_BROADCAST
) = range(6)


# --- UTILITY FUNCTIONS & DECORATORS ---

@contextmanager
def get_db():
    """Database session context manager."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

def load_admin_config():
    """Loads the admin configuration from a JSON file."""
    if not os.path.exists(ADMIN_CONFIG_FILE):
        return {}
    try:
        with open(ADMIN_CONFIG_FILE, 'r') as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.error(f"Error loading admin config: {e}")
        return {}

def save_admin_config(config):
    """Saves the admin configuration to a JSON file."""
    try:
        with open(ADMIN_CONFIG_FILE, 'w') as f:
            json.dump(config, f, indent=4)
    except IOError as e:
        logger.error(f"Error saving admin config: {e}")

def admin_only(func):
    """Decorator to restrict access to admin users only."""
    @wraps(func)
    async def wrapped(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        admin_config = load_admin_config()
        user_id = update.effective_user.id
        if not admin_config or str(user_id) != admin_config.get('admin_id'):
            await update.effective_message.reply_text("⛔️ Access denied. This command is for admins only.")
            return
        return await func(update, context, *args, **kwargs)
    return wrapped

# --- KEYBOARD LAYOUTS ---

def get_category_keyboard():
    """Returns the keyboard for selecting a feedback category."""
    keyboard = [
        [InlineKeyboardButton(category, callback_data=f"category_{category}")]
        for category in FEEDBACK_CATEGORIES
    ]
    return InlineKeyboardMarkup(keyboard)

def get_rating_keyboard():
    """Returns the keyboard for selecting a rating."""
    keyboard = [[InlineKeyboardButton("⭐" * i, callback_data=f"rating_{i}") for i in range(1, 6)]]
    return InlineKeyboardMarkup(keyboard)

def get_confirmation_keyboard():
    """Returns the keyboard for confirming feedback submission."""
    keyboard = [[
        InlineKeyboardButton("✅ Submit", callback_data="confirm_yes"),
        InlineKeyboardButton("❌ Cancel", callback_data="confirm_no"),
    ]]
    return InlineKeyboardMarkup(keyboard)

def get_admin_feedback_actions_keyboard(feedback_id, status):
    """Returns the keyboard for admin actions on a feedback item."""
    buttons = [
        InlineKeyboardButton("💬 Reply", callback_data=f"admin_reply_{feedback_id}"),
        InlineKeyboardButton("🗑️ Delete", callback_data=f"admin_delete_{feedback_id}"),
    ]
    if status != 'resolved':
        buttons.insert(1, InlineKeyboardButton("✅ Mark Resolved", callback_data=f"admin_resolve_{feedback_id}"))
    
    keyboard = [buttons, [InlineKeyboardButton("« Back to List", callback_data="admin_list_0")]]
    return InlineKeyboardMarkup(keyboard)


# --- USER CONVERSATION HANDLERS ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command for both users and admins."""
    user_id = update.effective_user.id
    admin_config = load_admin_config()

    if admin_config and str(user_id) == admin_config.get('admin_id'):
        await start_admin(update, context)
    else:
        # For regular users, show a button to start the feedback flow.
        # This prevents the conversation from auto-restarting.
        keyboard = [[InlineKeyboardButton("📝 Start New Feedback", callback_data="start_feedback")]]
        await update.effective_message.reply_text(
            "Welcome! Click the button below to leave feedback.",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    return ConversationHandler.END

async def start_user_flow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates the feedback submission process for a regular user via a button click."""
    query = update.callback_query
    await query.answer()
    
    welcome_text = (
        "<b>Welcome to the Feedback Center!</b>\n\n"
        "Your insights help us improve. Please select a category to begin."
    )
    await query.edit_message_text(
        welcome_text,
        reply_markup=get_category_keyboard(),
        parse_mode=constants.ParseMode.HTML
    )
    return SELECT_CATEGORY

async def select_category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user selecting a feedback category."""
    query = update.callback_query
    await query.answer()
    
    category = query.data.split("category_")[1]
    context.user_data['feedback_category'] = category
    
    # Updated prompt text
    await query.edit_message_text(
        f"<b>Category: {category}</b>\n\n"
        "Describe your feedback. You can also attach a photo, using the caption for your text.",
        parse_mode=constants.ParseMode.HTML
    )
    return GET_FEEDBACK_CONTENT

async def get_feedback_content_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the user sending their feedback content.
    This function now accepts either a text message or a photo (with an optional caption).
    """
    if update.message.photo:
        # User sent a photo
        context.user_data['feedback_photo_id'] = update.message.photo[-1].file_id
        # Use the caption as the message, or provide a default if it's empty
        context.user_data['feedback_message'] = update.message.caption or "(No description provided with photo)"
    elif update.message.text:
        # User sent a text message
        context.user_data['feedback_message'] = update.message.text
    else:
        # Should not happen with the current filters, but as a safeguard
        await update.message.reply_text("Unsupported message type. Please send text or a photo.")
        return GET_FEEDBACK_CONTENT

    # Transition to the rating step
    await update.message.reply_text(
        "Thank you! How would you rate your overall experience?",
        reply_markup=get_rating_keyboard()
    )
    return GET_RATING

async def get_rating_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the user selecting a rating and shows the confirmation summary."""
    query = update.callback_query
    await query.answer()
    
    rating = int(query.data.split("rating_")[1])
    context.user_data['feedback_rating'] = rating
    
    user_data = context.user_data
    summary = (
        "<b>📝 Please Confirm Your Feedback</b>\n\n"
        f"<b>Category:</b> {user_data['feedback_category']}\n"
        f"<b>Rating:</b> {'⭐' * rating}{'☆' * (5 - rating)}\n"
    )
    if user_data.get('feedback_photo_id'):
        summary += "<b>Attachment:</b> 🖼️ Image Attached\n"
    
    summary += (
        "\n<b>Message:</b>\n"
        f"<i>{user_data.get('feedback_message', 'No description provided.')}</i>\n\n"
        "Ready to submit?"
    )
    
    await query.message.delete()
    
    photo_id = context.user_data.get('feedback_photo_id')
    if photo_id:
        await context.bot.send_photo(
            chat_id=query.message.chat_id,
            photo=photo_id,
            caption=summary,
            reply_markup=get_confirmation_keyboard(),
            parse_mode=constants.ParseMode.HTML
        )
    else:
        await context.bot.send_message(
            chat_id=query.message.chat_id,
            text=summary,
            reply_markup=get_confirmation_keyboard(),
            parse_mode=constants.ParseMode.HTML
        )
    
    return CONFIRM_SUBMISSION

async def confirm_submission_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles submission, saves to DB, and notifies admin."""
    query = update.callback_query
    await query.answer()
    
    if query.data == 'confirm_no':
        await query.message.delete()
        await context.bot.send_message(chat_id=query.message.chat_id, text="Feedback submission cancelled.")
        context.user_data.clear()
        return ConversationHandler.END

    user = update.effective_user
    user_data = context.user_data
    
    try:
        with get_db() as db:
            new_feedback = Feedback(
                user_id=user.id,
                username=user.username or user.full_name,
                category=user_data['feedback_category'],
                message=user_data.get('feedback_message', 'No description provided.'),
                rating=user_data['feedback_rating'],
                photo_file_id=user_data.get('feedback_photo_id')
            )
            db.add(new_feedback)
            db.commit()
            feedback_id = new_feedback.id
    except SQLAlchemyError as e:
        logger.error(f"Database error on feedback submission: {e}")
        await query.message.delete()
        await context.bot.send_message(chat_id=query.message.chat_id, text="❌ A database error occurred. Please try again later.")
        return ConversationHandler.END

    final_text = (
        "✅ <b>Thank You!</b>\n\nYour feedback has been successfully submitted. "
        "We appreciate you taking the time to help us improve."
    )

    # Edit the final message and remove the keyboard to prevent re-clicks.
    if query.message.photo:
        await query.edit_message_caption(caption=final_text, reply_markup=None, parse_mode=constants.ParseMode.HTML)
    else:
        await query.edit_message_text(text=final_text, reply_markup=None, parse_mode=constants.ParseMode.HTML)
    
    await notify_admin_new_feedback(context, feedback_id)
    
    context.user_data.clear()
    return ConversationHandler.END

async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancels the current conversation."""
    await update.message.reply_text("Operation cancelled.")
    context.user_data.clear()
    return ConversationHandler.END

# --- ADMIN NOTIFICATION ---

async def notify_admin_new_feedback(context: ContextTypes.DEFAULT_TYPE, feedback_id: int):
    """Sends a notification to the admin, including a photo if available."""
    admin_config = load_admin_config()
    if not admin_config.get('admin_chat_id'):
        logger.warning(f"Admin not configured, cannot send notification for feedback #{feedback_id}")
        return

    with get_db() as db:
        fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
        if not fb: return

    user_info = f"@{fb.username}" if fb.username else f"User ID: {fb.user_id}"
    message_caption = (
        f"🚨 <b>New Feedback Received!</b> #{fb.id}\n\n"
        f"<b>From:</b> {user_info}\n"
        f"<b>Category:</b> {fb.category}\n"
        f"<b>Rating:</b> {'⭐' * fb.rating}\n\n"
        f"<b>Message:</b>\n<i>{fb.message}</i>"
    )
    
    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("View & Manage", callback_data=f"admin_view_{fb.id}")]])

    try:
        if fb.photo_file_id:
            await context.bot.send_photo(
                chat_id=admin_config['admin_chat_id'],
                photo=fb.photo_file_id,
                caption=message_caption,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=keyboard
            )
        else:
            await context.bot.send_message(
                chat_id=admin_config['admin_chat_id'],
                text=message_caption,
                parse_mode=constants.ParseMode.HTML,
                reply_markup=keyboard
            )
    except Exception as e:
        logger.error(f"Failed to send admin notification for feedback #{fb.id}: {e}")

# --- ADMIN HANDLERS ---

async def start_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays the admin welcome message and command list."""
    admin_menu = (
        "👑 <b>Admin Panel</b>\n\n"
        "Welcome! Here are your available commands:\n\n"
        "/listfb - 📬 View all feedback (paginated)\n"
        "/stats - 📊 Show feedback statistics\n"
        "/broadcast - 📢 Send a message to all users\n"
        "/help - ℹ️ Show this menu again"
    )
    await update.effective_message.reply_text(admin_menu, parse_mode=constants.ParseMode.HTML)

@admin_only
async def help_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Alias for the admin start menu."""
    await start_admin(update, context)

@admin_only
async def register_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Registers the user sending the command as the bot's admin."""
    user = update.effective_user
    admin_config = load_admin_config()
    
    if admin_config.get('admin_id') and str(user.id) != admin_config['admin_id']:
        await update.message.reply_text("❌ An admin is already registered. Only they can change the admin.")
        return

    new_config = {
        'admin_id': str(user.id),
        'admin_chat_id': str(update.message.chat_id),
        'admin_username': user.username or user.full_name
    }
    save_admin_config(new_config)
    
    await update.message.reply_text(
        "✅ <b>You are now the admin!</b>\n\n"
        "This chat will now receive all feedback notifications and admin commands will be available to you."
        , parse_mode=constants.ParseMode.HTML)

@admin_only
async def list_feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entry point for listing feedback, starts at page 0."""
    await display_feedback_page(update, context, page=0)

async def display_feedback_page(update: Update, context: ContextTypes.DEFAULT_TYPE, page: int):
    """Displays a paginated list of feedback for the admin."""
    with get_db() as db:
        total_feedback = db.query(Feedback).count()
        feedbacks = db.query(Feedback).order_by(Feedback.timestamp.desc()).offset(page * FEEDBACK_PER_PAGE).limit(FEEDBACK_PER_PAGE).all()

    if not feedbacks and page == 0:
        text = "No feedback has been submitted yet."
        markup = None
    elif not feedbacks:
        text = "You've reached the end of the feedback list."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to Start", callback_data="admin_list_0")]])
    else:
        text = f"📬 <b>Feedback List</b> (Page {page + 1})\n\n"
        for fb in feedbacks:
            status_icon = {'new': '🆕', 'replied': '💬', 'resolved': '✅'}.get(fb.status, '❓')
            photo_icon = " 🖼️" if fb.photo_file_id else ""
            text += (
                f"{status_icon} <b>#{fb.id}</b>: {fb.category}{photo_icon} ({'⭐' * fb.rating})\n"
                f"<i>{fb.message[:50]}...</i>\n"
                f"<a href='tg://user?id={fb.user_id}'>@{fb.username or 'user'}</a> - {fb.timestamp.strftime('%b %d, %Y')}\n"
                f"➡️ View: /view_{fb.id}\n\n"
            )
        
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton("« Prev", callback_data=f"admin_list_{page - 1}"))
        if (page + 1) * FEEDBACK_PER_PAGE < total_feedback:
            nav_buttons.append(InlineKeyboardButton("Next »", callback_data=f"admin_list_{page + 1}"))
        
        markup = InlineKeyboardMarkup([nav_buttons])

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)
    else:
        await update.effective_message.reply_text(text, reply_markup=markup, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

async def admin_list_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles pagination button clicks for the feedback list."""
    query = update.callback_query
    await query.answer()
    page = int(query.data.split('_')[-1])
    await display_feedback_page(update, context, page)

async def admin_view_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allows admin to view a specific feedback by ID, e.g., /view_123"""
    try:
        feedback_id = int(update.message.text.split('_')[1])
        await display_single_feedback(update, context, feedback_id)
    except (IndexError, ValueError):
        await update.message.reply_text("Invalid format. Use /view_ID, e.g., /view_123")
        
async def admin_view_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles 'View & Manage' button click."""
    query = update.callback_query
    await query.answer()
    feedback_id = int(query.data.split('_')[-1])
    await display_single_feedback(update, context, feedback_id)

async def display_single_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE, feedback_id: int):
    """Displays the photo (if any) and full details of a single feedback item."""
    with get_db() as db:
        fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()

    chat_id = update.effective_chat.id

    if not fb:
        text = f"Feedback with ID #{feedback_id} not found."
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("« Back to List", callback_data="admin_list_0")]])
        if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=markup)
        else: await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        return

    # FIX: Safely delete the previous message (like the list view) before sending the new one.
    if update.callback_query:
        try:
            await update.callback_query.message.delete()
        except BadRequest as e:
            if "Message to delete not found" in str(e):
                logger.warning("Tried to delete a message that was already gone.")
            else:
                raise e

    if fb.photo_file_id:
        try:
            await context.bot.send_photo(chat_id=chat_id, photo=fb.photo_file_id, caption=f"🖼️ Image attached to feedback #{fb.id}")
        except Exception as e:
            await context.bot.send_message(chat_id, f"Could not load image for feedback #{fb.id}. Error: {e}")

    status_icon = {'new': '🆕 New', 'replied': '💬 Replied', 'resolved': '✅ Resolved'}.get(fb.status, '❓')
    user_info = f"<a href='tg://user?id={fb.user_id}'>@{fb.username or 'user'}</a> (ID: {fb.user_id})"
    text = (
        f"<b>Feedback Details: #{fb.id}</b>\n\n"
        f"<b>Status:</b> {status_icon}\n"
        f"<b>Received:</b> {fb.timestamp.strftime('%b %d, %Y %H:%M')} UTC\n"
        f"<b>From:</b> {user_info}\n"
        f"<b>Category:</b> {fb.category}\n"
        f"<b>Rating:</b> {'⭐' * fb.rating}{'☆' * (5 - fb.rating)}\n\n"
        f"<b>Message:</b>\n<i>{fb.message}</i>"
    )
    markup = get_admin_feedback_actions_keyboard(feedback_id, fb.status)

    await context.bot.send_message(chat_id=chat_id, text=text, reply_markup=markup, parse_mode=constants.ParseMode.HTML, disable_web_page_preview=True)

async def admin_feedback_action_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles admin actions like reply, resolve, delete."""
    query = update.callback_query
    await query.answer()
    
    action, feedback_id_str = query.data.split('_', 2)[1:]
    feedback_id = int(feedback_id_str)

    if action == 'resolve':
        with get_db() as db:
            db.query(Feedback).filter(Feedback.id == feedback_id).update({'status': 'resolved'})
            db.commit()
        # FIX: Don't delete the message here. Let display_single_feedback handle it.
        await query.message.reply_text(f"✅ Feedback #{feedback_id} has been marked as resolved.")
        await display_single_feedback(update, context, feedback_id)

    elif action == 'delete':
        with get_db() as db:
            db.query(Feedback).filter(Feedback.id == feedback_id).delete()
            db.commit()
        await query.message.edit_text(f"🗑️ Feedback #{feedback_id} has been deleted.")

    elif action == 'reply':
        context.user_data['admin_reply_to_fb_id'] = feedback_id
        await query.message.reply_text(
            f"✍️ Please send your reply for feedback #{feedback_id}.\n\n"
            "The user will receive this message directly. To cancel, type /cancel."
        )
        return ADMIN_REPLY

async def admin_reply_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the message sent by the admin as a reply."""
    feedback_id = context.user_data.get('admin_reply_to_fb_id')
    if not feedback_id: return ConversationHandler.END

    reply_text = update.message.text
    
    with get_db() as db:
        fb = db.query(Feedback).filter(Feedback.id == feedback_id).first()
        if not fb:
            await update.message.reply_text("❌ Error: Could not find the original feedback.")
            return ConversationHandler.END
        user_id_to_reply = fb.user_id
        fb.status = 'replied'
        db.commit()

    user_message = f"📣 <b>A reply from the admin regarding your feedback:</b>\n\n<i>{reply_text}</i>"
    try:
        await context.bot.send_message(chat_id=user_id_to_reply, text=user_message, parse_mode=constants.ParseMode.HTML)
        await update.message.reply_text(f"✅ Your reply has been sent to the user for feedback #{feedback_id}.")
    except Exception as e:
        logger.error(f"Failed to send admin reply to user {user_id_to_reply}: {e}")
        await update.message.reply_text(f"❌ Could not send reply. The user may have blocked the bot. Feedback #{feedback_id} is now marked as 'replied'.")

    context.user_data.clear()
    return ConversationHandler.END

@admin_only
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays feedback statistics."""
    with get_db() as db:
        total = db.query(Feedback).count()
        if total == 0:
            await update.message.reply_text("No feedback yet to generate stats.")
            return

        avg_rating = db.query(func.avg(Feedback.rating)).scalar() or 0
        by_category = db.query(Feedback.category, func.count(Feedback.id)).group_by(Feedback.category).all()
        by_status = db.query(Feedback.status, func.count(Feedback.id)).group_by(Feedback.status).all()

    stats_text = "📊 <b>Feedback Statistics</b>\n\n"
    stats_text += f"▪️ Total Submissions: <b>{total}</b>\n"
    stats_text += f"▪️ Average Rating: <b>{avg_rating:.2f} / 5.0</b>\n\n"
    
    stats_text += "<b>By Category:</b>\n"
    stats_text += "\n".join([f"  - {cat}: {count}" for cat, count in by_category]) + "\n\n"
    
    stats_text += "<b>By Status:</b>\n"
    stats_text += "\n".join([f"  - {stat.capitalize()}: {count}" for stat, count in by_status])

    await update.message.reply_text(stats_text, parse_mode=constants.ParseMode.HTML)

@admin_only
async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Initiates the broadcast process."""
    await update.message.reply_text(
        "📢 <b>Broadcast Mode</b>\n\n"
        "Please send the message you want to broadcast to all users who have submitted feedback. "
        "Use /cancel to exit.",
        parse_mode=constants.ParseMode.HTML
    )
    return ADMIN_BROADCAST

async def broadcast_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Sends the broadcast message to all unique users."""
    broadcast_message = update.message.text
    await update.message.reply_text("Sending broadcast... Please wait.", parse_mode=constants.ParseMode.HTML)

    with get_db() as db:
        unique_users = db.query(Feedback.user_id).distinct().all()
        user_ids = [item[0] for item in unique_users]

    sent_count, failed_count = 0, 0
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=broadcast_message, parse_mode=constants.ParseMode.HTML)
            sent_count += 1
        except Exception as e:
            failed_count += 1
            logger.warning(f"Broadcast failed for user {user_id}: {e}")

    await update.message.reply_text(
        f"📢 <b>Broadcast Complete!</b>\n\n"
        f"✅ Messages sent: {sent_count}\n"
        f"❌ Messages failed: {failed_count}",
        parse_mode=constants.ParseMode.HTML
    )
    return ConversationHandler.END

# --- MAIN FUNCTION ---
def main():
    """Sets up and runs the bot."""
    if TOKEN == "YOUR_TELEGRAM_BOT_TOKEN":
        logger.error("FATAL: Telegram bot token is not set! Please set the TOKEN variable.")
        return

    application = Application.builder().token(TOKEN).build()

    # The main conversation for feedback submission
    feedback_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_user_flow, pattern="^start_feedback$")],
        states={
            SELECT_CATEGORY: [CallbackQueryHandler(select_category_callback, pattern="^category_")],
            GET_FEEDBACK_CONTENT: [MessageHandler(filters.TEXT | filters.PHOTO, get_feedback_content_handler)],
            GET_RATING: [CallbackQueryHandler(get_rating_callback, pattern="^rating_")],
            CONFIRM_SUBMISSION: [CallbackQueryHandler(confirm_submission_callback, pattern="^confirm_")],
        },
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_message=False
    )
    
    # Separate conversation for admin replies
    admin_reply_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_feedback_action_callback, pattern="^admin_reply_")],
        states={ADMIN_REPLY: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_reply_handler)]},
        fallbacks=[CommandHandler("cancel", cancel_handler)],
        per_message=False
    )

    # Separate conversation for admin broadcasts
    admin_broadcast_conv = ConversationHandler(
        entry_points=[CommandHandler("broadcast", broadcast_command)],
        states={ADMIN_BROADCAST: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_handler)]},
        fallbacks=[CommandHandler("cancel", cancel_handler)]
    )

    # Add all handlers
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(feedback_conv)
    application.add_handler(admin_reply_conv)
    application.add_handler(admin_broadcast_conv)

    # Admin commands
    application.add_handler(CommandHandler("registeradmin", register_admin_command))
    application.add_handler(CommandHandler("listfb", list_feedback_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("help", help_admin_command))
    application.add_handler(MessageHandler(filters.Regex(r'^\/view_\d+$'), admin_view_command))

    # General callback handlers for admin actions that don't start conversations
    application.add_handler(CallbackQueryHandler(admin_list_callback, pattern="^admin_list_"))
    application.add_handler(CallbackQueryHandler(admin_view_callback, pattern="^admin_view_"))
    application.add_handler(CallbackQueryHandler(admin_feedback_action_callback, pattern="^admin_(resolve|delete)_"))

    logger.info("Bot is starting...")
    application.run_polling()

if __name__ == '__main__':
    main()
