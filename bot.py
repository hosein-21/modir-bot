"""
Modir - ربات مدیریت گروه تلگرام
python-telegram-bot v21+ و PostgreSQL (asyncpg)

متغیرهای لازم: BOT_TOKEN ، DATABASE_URL
"""
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from functools import wraps

import asyncpg
from telegram import (
    ChatPermissions,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,