web: python -c "from db import init_db; init_db()" ; gunicorn app_v6:app --bind 0.0.0.0:$PORT --workers 1 --threads 8 --timeout 300
