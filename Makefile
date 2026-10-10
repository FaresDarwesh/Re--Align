.DEFAULT_GOAL := help
VENV := .venv
PY := $(VENV)/bin/python

help:
	@echo "RE-ALIGN — أوامر مساعدة"
	@echo "  make install      إنشاء البيئة وتثبيت الحزم"
	@echo "  make run          تشغيل السيرفر (تطوير)"
	@echo "  make seed         تعبئة بيانات تجريبية"
	@echo "  make test         تشغيل الاختبارات"
	@echo "  make security     فحص أمني شامل (ثغرات + كود + اختبارات)"
	@echo "  make harden       تقوية صلاحيات الملفات"
	@echo "  make migrate      ترقية قاعدة البيانات (Alembic)"
	@echo "  make revision m=\"رسالة\"   توليد ملف هجرة"
	@echo "  make admin        إنشاء/ترقية مستخدم إلى مدير عام"
	@echo "  make up           تشغيل كامل بـ Docker"
	@echo "  make backup       نسخ احتياطي لقاعدة البيانات"

$(VENV)/bin/activate: requirements.txt
	python3 -m venv $(VENV)
	$(VENV)/bin/pip install -q --upgrade pip
	$(VENV)/bin/pip install -q -r requirements.txt

install: $(VENV)/bin/activate

run: install
	$(VENV)/bin/uvicorn realign.main:app --host 0.0.0.0 --port 8000 --reload --reload-dir realign

seed: install
	$(PY) scripts/seed.py

test: install
	$(VENV)/bin/pytest -q

security: install
	./scripts/security_scan.sh

harden:
	./scripts/harden.sh

migrate: install
	$(VENV)/bin/alembic upgrade head

revision: install
	$(VENV)/bin/alembic revision --autogenerate -m "$(m)"

admin: install
	$(PY) scripts/create_admin.py

up:
	docker compose up -d --build

backup:
	./scripts/backup.sh
