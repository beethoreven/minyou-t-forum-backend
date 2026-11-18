"""正式環境的進入點。

    gunicorn wsgi:app --workers 1 --threads 8 --timeout 120

設定有問題時 create_app 會直接報錯，服務起不來，比帶著錯的設定上線好。
"""

from app import create_app

app = create_app()
