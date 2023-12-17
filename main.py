import os, dotenv, traceback
from loguru import logger
import pandas as pd
import json, html

import gspread_asyncio
from google.oauth2.service_account import Credentials 

from zoneinfo import ZoneInfo
from datetime import datetime, date, timedelta

from telegram import (
    Bot, Update,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    ContextTypes,
    CallbackContext,
    CallbackQueryHandler,
    Defaults,
)
from telegram.constants import ParseMode

dotenv.load_dotenv(dotenv.load_dotenv())

TZ            = os.environ['TZ']
BOT_TOKEN     = os.environ['BOT_TOKEN']
SHEETS_LINK   = os.environ['SHEETS_LINK']
SHEETS_ACC    = os.environ['SHEETS_ACC']
SCHELDUE_TIME = os.environ['SCHELDUE_TIME']
DEBUG         = os.environ['DEBUG']

RESULTS   = 'Results'
QUESTIONS = 'Questions'
PUBLISH   = 'Publish'
I18N      = 'I18n'

CALLBACK_PREFIX   = 'mg23_'
CALLBACK_TEMPLATE = 'mg23_{date}_{status}'
CALLBACK_PATTERN  = 'mg23_*'

def get_creds() -> Credentials:
    sheets_acc_json = json.loads(SHEETS_ACC)
    creds = Credentials.from_service_account_info(sheets_acc_json)
    scoped = creds.with_scopes([
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ])
    return scoped

class MgApplication(Application):
    async def init_sheets(self) -> None:
        self.agcm = gspread_asyncio.AsyncioGspreadClientManager(get_creds)
        self.agc  = await self.agcm.authorize()
        self.sh   = await self.agc.open_by_url(SHEETS_LINK)

        self.wks_results   = await self.sh.worksheet(RESULTS)
        self.wks_questions = await self.sh.worksheet(QUESTIONS)

        self.df_results   = pd.DataFrame(await self.wks_results.get_all_records())
        self.df_questions = pd.DataFrame(await self.wks_questions.get_all_records())
        self.df_results.user_id = self.df_results.user_id.apply(str)
        
        wks_publish = await self.sh.worksheet(PUBLISH)
        df_publish  = pd.DataFrame(await wks_publish.get_all_records())
        df_publish.chat_id = df_publish.chat_id.apply(str)
        self.admin_chat_id   = df_publish.loc[df_publish.is_active == 'admin'].iloc[0].chat_id
        self.publish_chat_id = df_publish.loc[df_publish.is_active == 'yes'].iloc[0].chat_id
        
        wks_i18n = await self.sh.worksheet(I18N)
        df_i18n  = pd.DataFrame(await wks_i18n.get_all_records())
        self.correct_text = df_i18n.loc[df_i18n.key == 'correct'].iloc[0].value
        self.wrong_text   = df_i18n.loc[df_i18n.key == 'wrong'].iloc[0].value
        self.late_text    = df_i18n.loc[df_i18n.key == 'late'].iloc[0].value
        self.answered_already_text = df_i18n.loc[df_i18n.key == 'answered_already'].iloc[0].value
    
    async def send_message_to_admin(self, text: str, parse_mode: str) -> None:
        bot: Bot = self.bot
        await bot.send_message(self.admin_chat_id, text=text, parse_mode=parse_mode)

    async def send_today_question(self) -> bool:
        today = date.today().strftime('%d.%m.%Y')
        
        question_df = self.df_questions.loc[self.df_questions.date == today]
        if question_df.empty:
            return False

        question = question_df.iloc[0]
        
        nrow, ncol = map(int, question.question_keyboard_size.split('x'))
        arow, acol = map(int, question.correct_answer.split('x'))
        
        reply_markup = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    text=f"{row+1}x{col+1}",
                    callback_data=CALLBACK_TEMPLATE.format(
                        date = today,
                        status = 'correct' if row == arow and col == acol else 'wrong'
                    )
                )
                for col in range(ncol)
            ]
            for row in range(nrow)
        ])

        bot: Bot = self.bot
        await bot.send_photo(
            chat_id = self.publish_chat_id,
            photo   = question.question_picture,
            caption = question.question_caption,
            reply_markup = reply_markup,
            parse_mode = ParseMode.MARKDOWN
        )
        return True
    
    async def send_today_answer(self) -> bool:
        yesterday_obj = date.today() - timedelta(days=1)
        yesterday = yesterday_obj.strftime('%d.%m.%Y')

        question_df = self.df_questions.loc[self.df_questions.date == yesterday]
        if question_df.empty:
            return False

        question = question_df.iloc[0]
        bot: Bot = self.bot
        await bot.send_photo(
            chat_id = self.publish_chat_id,
            photo   = question.answer_picture,
            caption = question.answer_caption,
            parse_mode = ParseMode.MARKDOWN
        )
        return True
    
    async def check_if_already_answered(self, user_id: str, date: str) -> bool:
        return not self.df_results.loc[
            (self.df_results.user_id == user_id) &
            (~self.df_results[date].isin(['', None]))
        ].empty

    async def set_answer_result(self, user_id: str, date: str, status: str) -> None:
        selector = self.df_results.user_id == user_id
        value = f"{status} {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        
        self.df_results.loc[selector, date] = value
        
        cell_row = self.df_results.loc[selector].index.values[0] + 2
        cell_col = self.df_results.columns.get_loc(date) + 1
        
        await self.wks_results.update_cell(cell_row, cell_col, value)

async def question_job(context: CallbackContext) -> None:
    app: MgApplication = context.application
    logger.info('Start scheldued job')
    
    if await app.send_today_answer():
        logger.info("Sent today answer")
    else:
        logger.info("No answer for today")
    
    if await app.send_today_question():
        logger.info("Sent today question")
    else:
        logger.info("No question for today")
    
    logger.info('Scheldued job done')

async def post_init(app: MgApplication) -> None:
    await app.init_sheets()
    logger.info('Spreadsheets initialized')
    
    if DEBUG == 'true':
        logger.info(f"Schelduing job straightaway")
        app.job_queue.run_once(question_job, when=2)
    else:
        time = datetime.strptime(SCHELDUE_TIME, '%H:%M').time()
        logger.info(f"Schelduing job daily at time {time}")
        app.job_queue.run_daily(question_job, time=time)
    logger.info("Job scheldued")

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: MgApplication = context.application

    user_id = str(update.effective_user.id)
    _, date, status = update.callback_query.data.split('_')

    log_str = f"for user {user_id} by date {date} with answer status {status}"
    logger.info(f"Got callback {log_str}")

    dead_hours, dead_mintes = map(int, SCHELDUE_TIME.split(':'))
    deadline = datetime.strptime(date, '%d.%m.%Y') + timedelta(days=1, hours=dead_hours, minutes=dead_mintes)
    now = datetime.now()

    if now > deadline:
        await update.callback_query.answer(app.late_text)
        logger.info(f"Too late {log_str}")
        return

    if await app.check_if_already_answered(user_id, date):
        logger.info(f"Already answered {log_str}")
        await update.callback_query.answer(app.answered_already_text)
        return

    status_i18n = app.correct_text if status == 'correct' else app.wrong_text
    await update.callback_query.answer(status_i18n)
    await app.set_answer_result(user_id, date, status)
    logger.info(f"Set result {log_str}")

async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    app: MgApplication = context.application

    tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
    tb_string = "".join(tb_list)

    logger.warning(f"Exception while handling an update: {tb_string}")

    update_str = update.to_dict() if isinstance(update, Update) else update if isinstance(update, dict) else str(update)
    messages_parts = [
        f"An exception was raised while handling an update",
        f"update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}",
        f"context.chat_data = {html.escape(str(context.chat_data))}",
        f"context.user_data = {html.escape(str(context.user_data))}",
        f"{html.escape(tb_string)}",
    ]
    messages = []
    for idx,message_part in enumerate(messages_parts):
        curr_len = len(message_part)
        template = "<pre>{message_part}</pre>\n\n" if idx > 0 else "{message_part}\n"
        if len(messages) > 0 and (len(messages[-1]) + curr_len <= 4096):
            messages[-1] += template.format(message_part=message_part)
        elif curr_len <= 4096:
            messages.append(template.format(message_part=message_part))
        else:
            for idx in range(0,curr_len,4096):
                messages.append(template.format(message_part=message_part[idx:idx+4096]))
    
    for message in messages:
        await app.send_message_to_admin(message, ParseMode.HTML)

if __name__ == '__main__':
    logger.info('Starting now...')
    app = ApplicationBuilder() \
        .application_class(MgApplication) \
        .token(BOT_TOKEN) \
        .post_init(post_init) \
        .defaults(Defaults(tzinfo=ZoneInfo(TZ))) \
        .build()

    app.add_error_handler(error_handler, block=False)
    app.add_handler(CallbackQueryHandler(callback_handler, pattern=CALLBACK_PATTERN, block=False))

    app.run_polling()
    logger.info('Done, have a greate day!')