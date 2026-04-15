web: python -c "from db import init_db; init_db()" && gunicorn app_v6:app --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 180
