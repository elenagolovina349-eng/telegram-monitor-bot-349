import sys
import warnings

warnings.filterwarnings("ignore")

# Патч для Python 3.13 - альтернативный подход
if sys.version_info >= (3, 13):
    try:
        import socket
        # Просто перезагружаем модуль socket, чтобы избежать ошибки
        import importlib

        importlib.reload(socket)
    except:
        pass

# Теперь импортируем остальные модули
import requests
from bs4 import BeautifulSoup
import hashlib
import time
import json
import schedule
import logging
from datetime import datetime
import difflib
import re
import sqlite3
from contextlib import contextmanager
from telegram import Bot, Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes
import asyncio
from threading import Thread, Lock
from collections import defaultdict
import random
import traceback
from urllib.parse import urlparse
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Отключим предупреждения SSL
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Настройка логирования
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ==================== БАЗА ДАННЫХ ====================

class DatabaseManager:
    def __init__(self, db_path="monitoring.db"):
        self.db_path = db_path
        self.lock = Lock()
        self.init_database()

    def init_database(self):
        with self.get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    subscribed INTEGER DEFAULT 0,
                    notification_level TEXT DEFAULT 'all',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS monitored_sites (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    url TEXT NOT NULL,
                    site_name TEXT,
                    css_selector TEXT,
                    last_hash TEXT,
                    last_content TEXT,
                    check_interval INTEGER DEFAULT 10,
                    enabled INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_checked TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id),
                    UNIQUE(user_id, url)
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_preferences (
                    user_id INTEGER PRIMARY KEY,
                    preferred_categories TEXT DEFAULT 'content,design,technical',
                    importance_weights TEXT DEFAULT '{"content": 1.0, "design": 0.7, "technical": 0.3}',
                    notification_frequency TEXT DEFAULT 'immediate',
                    learning_data TEXT DEFAULT '{"positive_feedback": 0, "negative_feedback": 0, "learned_patterns": {}}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS user_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    notification_id TEXT,
                    feedback_type TEXT,
                    change_category TEXT,
                    importance_level TEXT,
                    reaction_time INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS notification_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    notification_id TEXT,
                    site_name TEXT,
                    change_category TEXT,
                    importance_level TEXT,
                    content_hash TEXT,
                    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    feedback_received BOOLEAN DEFAULT FALSE,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')

            conn.execute('''
                CREATE TABLE IF NOT EXISTS check_errors (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    site_id INTEGER,
                    error_type TEXT,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (site_id) REFERENCES monitored_sites (id)
                )
            ''')

    @contextmanager
    def get_connection(self):
        with self.lock:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            try:
                yield conn
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.error(f"Database error: {e}")
                raise
            finally:
                conn.close()

    def get_user(self, user_id):
        with self.get_connection() as conn:
            return conn.execute(
                'SELECT * FROM users WHERE user_id = ?', (user_id,)
            ).fetchone()

    def create_user(self, user_id, username, first_name):
        with self.get_connection() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO users (user_id, username, first_name) VALUES (?, ?, ?)',
                (user_id, username, first_name)
            )

    def subscribe_user(self, user_id):
        with self.get_connection() as conn:
            conn.execute(
                'UPDATE users SET subscribed = 1 WHERE user_id = ?', (user_id,)
            )

    def unsubscribe_user(self, user_id):
        with self.get_connection() as conn:
            conn.execute(
                'UPDATE users SET subscribed = 0 WHERE user_id = ?', (user_id,)
            )

    def add_monitored_site(self, user_id, url, site_name, css_selector=None):
        with self.get_connection() as conn:
            try:
                conn.execute(
                    '''INSERT INTO monitored_sites 
                    (user_id, url, site_name, css_selector) 
                    VALUES (?, ?, ?, ?)''',
                    (user_id, url, site_name, css_selector)
                )
                return True
            except sqlite3.IntegrityError:
                logger.warning(f"Site {url} already exists for user {user_id}")
                return False

    def get_user_sites(self, user_id):
        with self.get_connection() as conn:
            return conn.execute(
                'SELECT * FROM monitored_sites WHERE user_id = ? AND enabled = 1', (user_id,)
            ).fetchall()

    def get_all_subscribed_users(self):
        with self.get_connection() as conn:
            return conn.execute(
                'SELECT * FROM users WHERE subscribed = 1'
            ).fetchall()

    def get_all_monitored_sites(self):
        with self.get_connection() as conn:
            return conn.execute('''
                SELECT ms.*, u.username, u.first_name 
                FROM monitored_sites ms 
                JOIN users u ON ms.user_id = u.user_id 
                WHERE u.subscribed = 1 AND ms.enabled = 1
                AND (ms.last_checked IS NULL OR 
                     datetime(ms.last_checked, '+' || ms.check_interval || ' minutes') < datetime('now'))
            ''').fetchall()

    def delete_site(self, user_id, site_id):
        with self.get_connection() as conn:
            conn.execute(
                'DELETE FROM monitored_sites WHERE id = ? AND user_id = ?',
                (site_id, user_id)
            )
            return conn.total_changes > 0

    def update_site_hash(self, site_id, current_hash, content):
        with self.get_connection() as conn:
            conn.execute(
                '''UPDATE monitored_sites 
                SET last_hash = ?, last_content = ?, last_checked = CURRENT_TIMESTAMP 
                WHERE id = ?''',
                (current_hash, content[:100000], site_id)
            )

    def record_error(self, site_id, error_type, error_message):
        with self.get_connection() as conn:
            conn.execute(
                '''INSERT INTO check_errors (site_id, error_type, error_message)
                VALUES (?, ?, ?)''',
                (site_id, error_type, error_message[:500])
            )


# ==================== СИСТЕМА УМНЫХ УВЕДОМЛЕНИЙ ====================

class UserPreferenceManager:
    def __init__(self, db_manager):
        self.db = db_manager

    def get_user_preferences(self, user_id):
        with self.db.get_connection() as conn:
            row = conn.execute(
                'SELECT * FROM user_preferences WHERE user_id = ?', (user_id,)
            ).fetchone()

            if row:
                return {
                    'preferred_categories': row['preferred_categories'].split(','),
                    'importance_weights': json.loads(row['importance_weights']),
                    'notification_frequency': row['notification_frequency'],
                    'learning_data': json.loads(row['learning_data'])
                }
            else:
                default_prefs = {
                    'preferred_categories': ['content', 'design', 'technical'],
                    'importance_weights': {'content': 1.0, 'design': 0.7, 'technical': 0.3},
                    'notification_frequency': 'immediate',
                    'learning_data': {
                        'positive_feedback': 0,
                        'negative_feedback': 0,
                        'learned_patterns': {}
                    }
                }
                self.update_user_preferences(user_id, default_prefs)
                return default_prefs

    def update_user_preferences(self, user_id, preferences):
        with self.db.get_connection() as conn:
            conn.execute('''
                INSERT OR REPLACE INTO user_preferences 
                (user_id, preferred_categories, importance_weights, notification_frequency, learning_data, updated_at)
                VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ''', (
                user_id,
                ','.join(preferences['preferred_categories']),
                json.dumps(preferences['importance_weights']),
                preferences['notification_frequency'],
                json.dumps(preferences['learning_data'])
            ))

    def record_feedback(self, user_id, notification_id, feedback_type, change_data):
        with self.db.get_connection() as conn:
            conn.execute('''
                INSERT INTO user_feedback 
                (user_id, notification_id, feedback_type, change_category, importance_level)
                VALUES (?, ?, ?, ?, ?)
            ''', (user_id, notification_id, feedback_type,
                  change_data.get('category'), change_data.get('importance')))

            conn.execute('''
                UPDATE notification_history 
                SET feedback_received = TRUE 
                WHERE notification_id = ? AND user_id = ?
            ''', (notification_id, user_id))

        self.update_learning_model(user_id, feedback_type, change_data)

    def update_learning_model(self, user_id, feedback_type, change_data):
        prefs = self.get_user_preferences(user_id)
        learning_data = prefs['learning_data']

        category = change_data.get('category')
        importance = change_data.get('importance')

        if feedback_type == 'like':
            learning_data['positive_feedback'] += 1
        elif feedback_type in ['dislike', 'dismiss']:
            learning_data['negative_feedback'] += 1

        key = f"{category}_{importance}"
        if key not in learning_data['learned_patterns']:
            learning_data['learned_patterns'][key] = {'likes': 0, 'dislikes': 0}

        if feedback_type == 'like':
            learning_data['learned_patterns'][key]['likes'] += 1
        elif feedback_type in ['dislike', 'dismiss']:
            learning_data['learned_patterns'][key]['dislikes'] += 1

        self.adjust_importance_weights(user_id, learning_data)
        prefs['learning_data'] = learning_data
        self.update_user_preferences(user_id, prefs)

    def adjust_importance_weights(self, user_id, learning_data):
        prefs = self.get_user_preferences(user_id)
        weights = prefs['importance_weights']

        for pattern_key, feedback in learning_data['learned_patterns'].items():
            category = pattern_key.split('_')[0]
            total_feedback = feedback['likes'] + feedback['dislikes']

            if total_feedback >= 3:
                like_ratio = feedback['likes'] / total_feedback

                if like_ratio > 0.7:
                    weights[category] = min(1.0, weights[category] + 0.1)
                elif like_ratio < 0.3:
                    weights[category] = max(0.1, weights[category] - 0.1)

        prefs['importance_weights'] = weights
        self.update_user_preferences(user_id, prefs)

    def should_send_notification(self, user_id, change_analysis):
        prefs = self.get_user_preferences(user_id)

        category = change_analysis.get('category')
        if category not in prefs['preferred_categories']:
            return False

        importance = change_analysis.get('importance', 'medium')
        base_importance_score = {'high': 1.0, 'medium': 0.5, 'low': 0.2}[importance]
        user_weight = prefs['importance_weights'].get(category, 0.5)

        final_score = base_importance_score * user_weight
        threshold = 0.3

        return final_score >= threshold


class AINotificationFilter:
    def __init__(self, api_key=None):
        self.api_url = "https://api.aitunnel.ru/v1/chat/completions"
        self.api_key = api_key

    def analyze_change_importance(self, change_data, user_context=None):
        if not self.api_key:
            return self._basic_analysis(change_data, user_context)

        try:
            prompt = self._create_importance_prompt(change_data, user_context)

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            data = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 600
            }

            response = requests.post(self.api_url, headers=headers, json=data, timeout=30)
            response.raise_for_status()

            result = response.json()
            analysis_text = result['choices'][0]['message']['content']

            return json.loads(analysis_text)

        except Exception as e:
            logger.error(f"AI analysis error: {e}")
            return self._basic_analysis(change_data, user_context)

    def _create_importance_prompt(self, change_data, user_context):
        user_info = user_context or {}

        return f"""
        Ты - умный фильтр уведомлений. Проанализируй изменения на сайте и определи их важность для конкретного пользователя.

        ДАННЫЕ ПОЛЬЗОВАТЕЛЯ:
        - Имя: {user_info.get('first_name', 'Пользователь')}
        - Предпочтения: {user_info.get('preferred_categories', ['content', 'design', 'technical'])}
        - Активность: {user_info.get('activity_level', 'средняя')}

        ИНФОРМАЦИЯ О ИЗМЕНЕНИЯХ:
        - Сайт: {change_data['site_name']}
        - URL: {change_data['url']}
        - Тип изменений: {change_data.get('change_type', 'неизвестно')}
        - Дифф изменений: {change_data['diff'][:1000]}

        ПРОАНАЛИЗИРУЙ:
        1. Категория изменений (content/design/technical/commerce/news)
        2. Уровень важности (high/medium/low)
        3. Насколько это важно для ЭТОГО пользователя
        4. Ключевые аспекты изменений
        5. Рекомендация по отправке уведомления

        ФОРМАТ ОТВЕТА (JSON):
        {{
            "category": "content/design/technical/commerce/news",
            "importance": "high/medium/low",
            "personal_importance_score": 0.85,
            "key_aspects": ["аспект1", "аспект2", "аспект3"],
            "should_notify": true/false,
            "reasoning": "Обоснование решения",
            "personalized_summary": "Краткое описание для пользователя"
        }}
        """

    def _basic_analysis(self, change_data, user_context):
        change_size = len(change_data['diff'])

        if change_size > 2000:
            importance = "high"
            category = "content"
        elif change_size > 500:
            importance = "medium"
            category = "design"
        else:
            importance = "low"
            category = "technical"

        return {
            "category": category,
            "importance": importance,
            "personal_importance_score": 0.5,
            "key_aspects": [f"Изменения размером {change_size} символов"],
            "should_notify": True,
            "reasoning": "Базовый анализ на основе размера изменений",
            "personalized_summary": f"Обнаружены изменения на сайте {change_data['site_name']}"
        }

    def generate_personalized_message(self, change_analysis, user_preferences):
        if not self.api_key:
            return self._basic_message(change_analysis, user_preferences)

        try:
            prompt = self._create_message_prompt(change_analysis, user_preferences)

            headers = {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json"
            }

            data = {
                "model": "gpt-3.5-turbo",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.7,
                "max_tokens": 500
            }

            response = requests.post(self.api_url, headers=headers, json=data, timeout=30)
            response.raise_for_status()

            result = response.json()
            return result['choices'][0]['message']['content']

        except Exception as e:
            logger.error(f"Message generation error: {e}")
            return self._basic_message(change_analysis, user_preferences)

    def _create_message_prompt(self, change_analysis, user_preferences):
        return f"""
        Создай персонализированное уведомление об изменениях на сайте.

        АНАЛИЗ ИЗМЕНЕНИЙ:
        - Категория: {change_analysis['category']}
        - Важность: {change_analysis['importance']}
        - Ключевые аспекты: {', '.join(change_analysis['key_aspects'])}
        - Сайт: {change_analysis.get('site_name', 'неизвестно')}

        ПРЕДПОЧТЕНИЯ ПОЛЬЗОВАТЕЛЯ:
        - Любимые категории: {user_preferences.get('preferred_categories', [])}
        - Веса важности: {user_preferences.get('importance_weights', {})}

        СОЗДАЙ УВЕДОМЛЕНИЕ КОТОРОЕ:
        1. Учитывает предпочтения пользователя
        2. Выделяет самое важное для него
        3. Имеет персонализированный тон
        4. Краткое и информативное (2-3 предложения)
        5. Включает призыв к действию

        Ответь только текстом уведомления без дополнительных пояснений.
        """

    def _basic_message(self, change_analysis, user_preferences):
        return f"Обнаружены изменения на сайте. Категория: {change_analysis['category']}, Важность: {change_analysis['importance']}"


class NotificationGrouper:
    def __init__(self):
        self.pending_notifications = defaultdict(list)

    def add_notification(self, user_id, notification):
        self.pending_notifications[user_id].append(notification)

    def get_grouped_notifications(self, user_id, time_window_minutes=30):
        notifications = self.pending_notifications.get(user_id, [])

        if not notifications:
            return []

        grouped = defaultdict(list)
        for notification in notifications:
            key = f"{notification['category']}_{notification['site_name']}"
            grouped[key].append(notification)

        summary_notifications = []
        for key, group in grouped.items():
            if len(group) == 1:
                summary_notifications.append(group[0])
            else:
                summary_notifications.append(
                    self._create_summary_notification(group)
                )

        self.pending_notifications[user_id] = []
        return summary_notifications

    def _create_summary_notification(self, notifications):
        main_notification = notifications[0]

        return {
            'site_name': main_notification['site_name'],
            'category': main_notification['category'],
            'importance': max(n['importance'] for n in notifications),
            'title': f"🔔 {len(notifications)} обновлений на {main_notification['site_name']}",
            'message': f"За последнее время произошло {len(notifications)} изменений в категории {main_notification['category']}.",
            'is_summary': True,
            'detailed_changes': [n['personalized_summary'] for n in notifications],
            'notification_id': f"summary_{datetime.now().timestamp()}",
            'timestamp': datetime.now()
        }


class SmartNotificationSystem:
    def __init__(self, telegram_bot, db_manager, ai_api_key=None):
        self.bot = telegram_bot
        self.db = db_manager
        self.preference_manager = UserPreferenceManager(db_manager)
        self.ai_filter = AINotificationFilter(ai_api_key)
        self.grouper = NotificationGrouper()

        self.stats = {
            'notifications_sent': 0,
            'notifications_filtered': 0,
            'user_feedback': {'likes': 0, 'dislikes': 0}
        }

    async def process_change(self, user_id, site_info, change_data):
        try:
            user_prefs = self.preference_manager.get_user_preferences(user_id)
            user_context = {
                'first_name': 'Пользователь',
                'preferred_categories': user_prefs['preferred_categories'],
                'activity_level': 'средняя'
            }

            change_analysis = self.ai_filter.analyze_change_importance(
                {**site_info, **change_data}, user_context
            )

            should_send = self.preference_manager.should_send_notification(
                user_id, change_analysis
            )

            if not should_send:
                self.stats['notifications_filtered'] += 1
                logger.info(f"Notification filtered for user {user_id}")
                return

            personalized_message = self.ai_filter.generate_personalized_message(
                change_analysis, user_prefs
            )

            notification = {
                'notification_id': f"{user_id}_{datetime.now().timestamp()}",
                'site_name': site_info['site_name'],
                'category': change_analysis['category'],
                'importance': change_analysis['importance'],
                'title': self._get_notification_title(change_analysis),
                'message': personalized_message,
                'personalized_summary': change_analysis['personalized_summary'],
                'timestamp': datetime.now()
            }

            self.grouper.add_notification(user_id, notification)
            await self._send_grouped_notifications(user_id)

            self.stats['notifications_sent'] += 1
            logger.info(f"Notification sent to user {user_id} for {site_info['site_name']}")

        except Exception as e:
            logger.error(f"Error processing change: {e}")

    async def _send_grouped_notifications(self, user_id):
        grouped_notifications = self.grouper.get_grouped_notifications(user_id)

        for notification in grouped_notifications:
            await self._send_single_notification(user_id, notification)

    async def _send_single_notification(self, user_id, notification):
        message = self._format_notification_message(notification)

        keyboard = [
            [
                InlineKeyboardButton("👍", callback_data=f"like_{notification['notification_id']}"),
                InlineKeyboardButton("👎", callback_data=f"dislike_{notification['notification_id']}"),
                InlineKeyboardButton("❌", callback_data=f"dismiss_{notification['notification_id']}")
            ]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)

        try:
            await self.bot.send_message(
                chat_id=user_id,
                text=message,
                parse_mode='Markdown',
                reply_markup=reply_markup
            )

            self._save_notification_history(user_id, notification)
            logger.info(f"Notification delivered to {user_id}")

        except Exception as e:
            logger.error(f"Error sending notification: {e}")

    def _get_notification_title(self, analysis):
        emoji = {"high": "🔴", "medium": "🟡", "low": "🔵"}.get(analysis['importance'], "⚪")
        category_names = {
            "content": "Контент", "design": "Дизайн",
            "technical": "Техническое", "commerce": "Коммерция", "news": "Новости"
        }
        return f"{emoji} Обновление {category_names.get(analysis['category'], '')}"

    def _format_notification_message(self, notification):
        if notification.get('is_summary'):
            message = f"*{notification['title']}*\n\n{notification['message']}\n\n"
            message += "*Детали изменений:*\n"
            for change in notification['detailed_changes']:
                message += f"• {change}\n"
        else:
            message = f"*{notification['title']}*\n\n{notification['message']}"

        message += f"\n_Время: {notification['timestamp'].strftime('%H:%M %d.%m.%Y')}_"
        return message

    def _save_notification_history(self, user_id, notification):
        try:
            with self.db.get_connection() as conn:
                conn.execute('''
                    INSERT INTO notification_history 
                    (user_id, notification_id, site_name, change_category, importance_level)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, notification['notification_id'], notification['site_name'],
                      notification['category'], notification['importance']))
        except Exception as e:
            logger.error(f"Error saving notification history: {e}")

    async def handle_feedback(self, user_id, notification_id, feedback_type):
        try:
            with self.db.get_connection() as conn:
                notification = conn.execute('''
                    SELECT * FROM notification_history 
                    WHERE notification_id = ? AND user_id = ?
                ''', (notification_id, user_id)).fetchone()

            if notification:
                change_data = {
                    'category': notification['change_category'],
                    'importance': notification['importance_level']
                }

                self.preference_manager.record_feedback(
                    user_id, notification_id, feedback_type, change_data
                )

                if feedback_type == 'like':
                    self.stats['user_feedback']['likes'] += 1
                elif feedback_type in ['dislike', 'dismiss']:
                    self.stats['user_feedback']['dislikes'] += 1

                logger.info(f"Feedback recorded: {feedback_type} from user {user_id}")
        except Exception as e:
            logger.error(f"Error handling feedback: {e}")


# ==================== TELEGRAM BOT ====================

class MonitoringBot:
    def __init__(self, token, ai_api_key=None):
        self.application = Application.builder().token(token).build()
        self.db = DatabaseManager()
        self.notification_system = SmartNotificationSystem(
            self.application.bot, self.db, ai_api_key
        )
        self.monitoring_active = True

        self.setup_handlers()

    def setup_handlers(self):
        self.application.add_handler(CommandHandler("start", self.start_command))
        self.application.add_handler(CommandHandler("subscribe", self.subscribe_command))
        self.application.add_handler(CommandHandler("unsubscribe", self.unsubscribe_command))
        self.application.add_handler(CommandHandler("status", self.status_command))
        self.application.add_handler(CommandHandler("monitor", self.monitor_command))
        self.application.add_handler(CommandHandler("mysites", self.mysites_command))
        self.application.add_handler(CommandHandler("recommend", self.recommend_command))
        self.application.add_handler(CommandHandler("delete", self.delete_command))
        self.application.add_handler(CallbackQueryHandler(self.handle_callback))

    async def start_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        self.db.create_user(user.id, user.username, user.first_name)

        welcome_text = f"""
👋 Привет, {user.first_name}!

Я - умный бот для мониторинга сайтов с ИИ-анализом.

📋 *Доступные команды:*
/subscribe - Подписаться на уведомления
/unsubscribe - Отписаться от уведомлений  
/status - Статус мониторинга
/monitor [url] - Начать мониторинг сайта
/mysites - Мои сайты для мониторинга
/delete - Удалить сайт из мониторинга
/recommend - Персональные рекомендации

🚀 Начни с команды /monitor чтобы добавить первый сайт!
        """

        await update.message.reply_text(welcome_text, parse_mode='Markdown')

    async def subscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        self.db.subscribe_user(user.id)

        await update.message.reply_text(
            "✅ Вы успешно подписались на уведомления!\n"
            "Теперь вы будете получать умные уведомления об изменениях на сайтах.",
            parse_mode='Markdown'
        )

    async def unsubscribe_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        self.db.unsubscribe_user(user.id)

        await update.message.reply_text(
            "🔕 Вы отписались от уведомлений.\n"
            "Чтобы снова получать уведомления, используйте /subscribe",
            parse_mode='Markdown'
        )

    async def status_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_data = self.db.get_user(user.id)
        user_sites = self.db.get_user_sites(user.id)
        all_sites = self.db.get_all_monitored_sites()

        status_text = f"""
📊 *Статус мониторинга*

👤 *Пользователь:* {user.first_name}
🔔 *Подписка:* {'✅ Активна' if user_data and user_data['subscribed'] else '❌ Неактивна'}
🌐 *Ваших сайтов:* {len(user_sites)}
📈 *Всего сайтов в системе:* {len(all_sites)}
🔄 *Система мониторинга:* {'✅ Активна' if self.monitoring_active else '⏸️ Остановлена'}

💡 Используйте /monitor чтобы добавить сайты
💡 Используйте /delete чтобы удалить сайты
        """

        await update.message.reply_text(status_text, parse_mode='Markdown')

    async def monitor_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not context.args:
            await update.message.reply_text(
                "❌ Укажите URL сайта для мониторинга:\n"
                "Пример: /monitor https://example.com"
            )
            return

        url = context.args[0]
        user = update.effective_user

        if not url.startswith(('http://', 'https://')):
            url = 'https://' + url

        try:
            parsed_url = urlparse(url)
            if not parsed_url.netloc:
                await update.message.reply_text("❌ Неверный URL. Укажите полный адрес сайта.")
                return

            domain = parsed_url.netloc
            site_name = domain.replace('www.', '')

            success = self.db.add_monitored_site(user.id, url, site_name)

            if success:
                await update.message.reply_text(
                    f"✅ Сайт добавлен в мониторинг!\n"
                    f"🌐 *Сайт:* {site_name}\n"
                    f"🔗 *URL:* {url}\n\n"
                    f"Теперь используйте /subscribe чтобы получать уведомления об изменениях.",
                    parse_mode='Markdown'
                )
            else:
                await update.message.reply_text(
                    f"ℹ️ Сайт {site_name} уже добавлен в мониторинг."
                )

        except Exception as e:
            logger.error(f"Error adding site: {e}")
            await update.message.reply_text(
                f"❌ Ошибка добавления сайта: {str(e)}"
            )

    async def mysites_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_sites = self.db.get_user_sites(user.id)

        if not user_sites:
            await update.message.reply_text(
                "📭 У вас нет сайтов в мониторинге.\n"
                "Добавьте сайты командой /monitor [url]"
            )
            return

        sites_text = "🌐 *Ваши сайты в мониторинге:*\n\n"
        for site in user_sites:
            last_checked = site['last_checked'] or 'никогда'
            sites_text += f"• {site['site_name']}\n  {site['url']}\n  Последняя проверка: {last_checked}\n\n"

        await update.message.reply_text(sites_text, parse_mode='Markdown')

    async def delete_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_sites = self.db.get_user_sites(user.id)

        if not user_sites:
            await update.message.reply_text(
                "📭 У вас нет сайтов в мониторинге.\n"
                "Добавьте сайты командой /monitor [url]"
            )
            return

        keyboard = []
        for site in user_sites:
            keyboard.append([
                InlineKeyboardButton(
                    f"🗑️ {site['site_name']}",
                    callback_data=f"delete_{site['id']}"
                )
            ])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "🗑️ *Выберите сайт для удаления:*",
            reply_markup=reply_markup,
            parse_mode='Markdown'
        )

    async def recommend_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        user_sites = self.db.get_user_sites(user.id)

        if not user_sites:
            await update.message.reply_text(
                "📭 Сначала добавьте сайты в мониторинг командой /monitor"
            )
            return

        recommendations = [
            "💡 *Совет 1:* Мониторьте сайты в разное время суток",
            "💡 *Совет 2:* Добавляйте CSS-селекторы для точного мониторинга",
            "💡 *Совет 3:* Используйте лайки/дизлайки чтобы обучить ИИ",
            "💡 *Совет 4:* Начинайте с 2-3 сайтов, затем добавляйте больше",
            "💡 *Совет 5:* Проверяйте статус мониторинга командой /status"
        ]

        await update.message.reply_text(
            f"🎯 *Персональные рекомендации:*\n\n{random.choice(recommendations)}",
            parse_mode='Markdown'
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()

        user_id = query.from_user.id
        data = query.data

        if data.startswith('delete_'):
            site_id = int(data.split('_')[1])
            if self.db.delete_site(user_id, site_id):
                await query.edit_message_text("✅ Сайт удален из мониторинга!")
            else:
                await query.edit_message_text("❌ Не удалось удалить сайт")

        elif data.startswith(('like_', 'dislike_', 'dismiss_')):
            feedback_type, notification_id = data.split('_', 1)
            await self.notification_system.handle_feedback(
                user_id, notification_id, feedback_type
            )

    def run(self):
        """Запуск бота"""
        print("🤖 Запуск Telegram бота...")
        self.application.run_polling()


# ==================== СИСТЕМА МОНИТОРИНГА ====================

class SmartMonitoringSystem:
    def __init__(self, db_manager, notification_system):
        self.db = db_manager
        self.notification_system = notification_system
        self.active = True
        self.session = self._create_session()

    def _create_session(self):
        """Создает сессию requests с повторными попытками"""
        session = requests.Session()

        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["HEAD", "GET", "OPTIONS"]
        )

        adapter = HTTPAdapter(max_retries=retry_strategy, pool_connections=100, pool_maxsize=100)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        return session

    def start_monitoring(self, interval_minutes=10):
        self.active = True
        logger.info(f"🚀 Запуск системы мониторинга (интервал: {interval_minutes} минут)")

        def monitoring_loop():
            while self.active:
                try:
                    schedule.run_pending()
                    time.sleep(1)
                except Exception as e:
                    logger.error(f"Error in monitoring loop: {e}")

        Thread(target=monitoring_loop, daemon=True).start()

        Thread(target=self.check_all_sites, daemon=True).start()

        schedule.every(interval_minutes).minutes.do(self.check_all_sites)

    def stop_monitoring(self):
        self.active = False
        schedule.clear()
        logger.info("🛑 Мониторинг остановлен")

    def check_all_sites(self):
        if not self.active:
            return

        logger.info(f"🔍 Проверка сайтов... {datetime.now().strftime('%H:%M:%S')}")

        try:
            sites = self.db.get_all_monitored_sites()
            logger.info(f"Найдено {len(sites)} сайтов для проверки")

            for site in sites:
                try:
                    self.check_site(site)
                    time.sleep(random.uniform(2, 5))
                except Exception as e:
                    logger.error(f"Error checking site {site['url']}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error in check_all_sites: {e}")

    def check_site(self, site):
        try:
            logger.info(f"Проверка сайта: {site['site_name']} ({site['url']})")

            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ]

            headers = {
                'User-Agent': random.choice(user_agents),
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                'Accept-Encoding': 'gzip, deflate',
                'Connection': 'keep-alive',
                'Upgrade-Insecure-Requests': '1',
                'DNT': '1',
                'Cache-Control': 'max-age=0'
            }

            time.sleep(random.uniform(1, 3))

            try:
                response = self.session.get(
                    site['url'],
                    headers=headers,
                    timeout=30,
                    verify=False
                )
                response.raise_for_status()

                if response.encoding:
                    content = response.text
                else:
                    content = response.content.decode('utf-8', errors='ignore')

            except requests.exceptions.SSLError:
                response = self.session.get(
                    site['url'].replace('https://', 'http://'),
                    headers=headers,
                    timeout=30,
                    verify=False
                )
                response.raise_for_status()
                content = response.text

            soup = BeautifulSoup(content, 'lxml', from_encoding='utf-8')

            for element in soup(["script", "style", "meta", "link", "noscript", "iframe"]):
                element.decompose()

            try:
                text = soup.get_text()
                lines = (line.strip() for line in text.splitlines())
                chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
                text = ' '.join(chunk for chunk in chunks if chunk)
            except Exception as e:
                logger.error(f"Error parsing text for {site['url']}: {e}")
                self.db.record_error(site['id'], 'parsing_error', str(e))
                return

            if not text or len(text) < 50:
                logger.warning(f"Слишком мало текста на сайте {site['site_name']}")
                return

            current_hash = hashlib.md5(text.encode('utf-8', errors='ignore')).hexdigest()

            if site['last_hash'] and current_hash != site['last_hash']:
                logger.info(f"🔄 Обнаружены изменения на {site['site_name']}")

                old_content = site['last_content'] or ''
                old_content_str = old_content if isinstance(old_content, str) else str(old_content)

                try:
                    changes_diff = '\n'.join(difflib.unified_diff(
                        old_content_str.splitlines(),
                        text.splitlines(),
                        lineterm='',
                        n=2
                    ))[:5000]
                except Exception as e:
                    logger.error(f"Error creating diff: {e}")
                    changes_diff = "Изменения обнаружены, но не могут быть отображены"

                changes_data = {
                    'diff': changes_diff,
                    'old_content': old_content_str,
                    'new_content': text,
                    'change_type': 'content_update'
                }

                site_info = {
                    'site_name': site['site_name'],
                    'url': site['url']
                }

                try:
                    asyncio.run_coroutine_threadsafe(
                        self.notification_system.process_change(
                            site['user_id'], site_info, changes_data
                        ),
                        asyncio.get_event_loop()
                    )
                except Exception as e:
                    logger.error(f"Error scheduling notification: {e}")

            self.db.update_site_hash(site['id'], current_hash, text)
            logger.info(f"✅ Сайт {site['site_name']} успешно проверен")

        except requests.exceptions.HTTPError as e:
            error_msg = f"HTTP {e.response.status_code}"
            logger.warning(f"{error_msg} для {site['url']}")
            self.db.record_error(site['id'], 'http_error', error_msg)

        except requests.exceptions.Timeout:
            error_msg = "Timeout"
            logger.warning(f"Таймаут для {site['url']}")
            self.db.record_error(site['id'], 'timeout', error_msg)

        except requests.exceptions.ConnectionError:
            error_msg = "Connection error"
            logger.warning(f"Ошибка соединения с {site['url']}")
            self.db.record_error(site['id'], 'connection_error', error_msg)

        except Exception as e:
            error_msg = str(e)
            logger.error(f"❌ Ошибка проверки {site['url']}: {error_msg}")
            self.db.record_error(site['id'], 'general_error', error_msg)


# ==================== ЗАПУСК СИСТЕМЫ ====================

def main():
    # ⭐⭐ ВСТАВЬ СВОЙ ТОКЕН ЗДЕСЬ ⭐⭐
    TELEGRAM_BOT_TOKEN = "8290004533:AAGFtpkPb8-bWvvR0iAK2oCKW1GF5R0GikU"  # ← ЗАМЕНИТЕ ЭТО НА ВАШ ТОКЕН
    AI_TUNNEL_API_KEY = ""

    if TELEGRAM_BOT_TOKEN == "YOUR_TELEGRAM_BOT_TOKEN_HERE":
        print("❌ ОШИБКА: Ты не вставил свой токен!")
        print("📝 Как получить токен:")
        print("1. Найди @BotFather в Telegram")
        print("2. Отправь /newbot")
        print("3. Придумай имя бота")
        print("4. Скопируй токен (выглядит так: 654321987:AAFfdfdffdffdfdffdfdffdfdffdfdff)")
        print("5. Вставь его вместо 'YOUR_TELEGRAM_BOT_TOKEN_HERE'")
        print("6. Перезапусти программу")
        return

    try:
        db = DatabaseManager()
        bot = MonitoringBot(TELEGRAM_BOT_TOKEN, AI_TUNNEL_API_KEY)
        monitoring_system = SmartMonitoringSystem(db, bot.notification_system)

        monitoring_thread = Thread(target=monitoring_system.start_monitoring, args=(10,), daemon=True)
        monitoring_thread.start()

        bot.run()

    except KeyboardInterrupt:
        print("\n🛑 Остановка системы...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        print(f"❌ Критическая ошибка: {e}")


if __name__ == "__main__":
    main()