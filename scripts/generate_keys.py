#!/usr/bin/env python3
"""ولّد المفاتيح السرية واطبعها لتُنسخ إلى ملف .env"""
from cryptography.fernet import Fernet
import secrets

print("# الصق السطرين التاليين في ملف .env")
print(f'APP_SECRET_KEY="{secrets.token_urlsafe(64)}"')
print(f'CREDENTIALS_ENCRYPTION_KEY="{Fernet.generate_key().decode()}"')
