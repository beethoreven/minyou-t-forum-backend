#!/bin/bash
# 本機開發與測試用的 PostgreSQL 18，資料放在 .localdb/，port 5442。
# 不是系統服務：電腦重開機，或執行 tools/local_db.sh stop 之後就停了。
#
#   tools/local_db.sh start    # 第一次執行會建立資料庫，以及 minyou_t_forum_dev、minyou_t_forum_test
#   tools/local_db.sh stop
#
# 需要 PostgreSQL 18：brew install postgresql@18
set -euo pipefail
export LC_ALL=C
cd "$(dirname "$0")/.."
PGBIN="${PGBIN:-$(brew --prefix postgresql@18)/bin}"
DATA=.localdb
PORT=5442

case "${1:-start}" in
  start)
    if [ ! -d "$DATA" ]; then
      # --auth=trust：本機連線不用密碼。這個資料庫只聽 localhost，裡面也只有測試資料。
      "$PGBIN/initdb" -D "$DATA" -U postgres --auth=trust --encoding=UTF8 --locale=C >/dev/null
    fi
    # 已經在跑就不要再啟動一次（再啟動會報錯）。
    if ! "$PGBIN/pg_ctl" -D "$DATA" status >/dev/null 2>&1; then
      "$PGBIN/pg_ctl" -D "$DATA" -o "-p $PORT -k /tmp" -l "$DATA/server.log" -w start
    fi
    # 資料庫的名稱用底線：PostgreSQL 不加引號的名稱不能有連字號。
    for db in minyou_t_forum_dev minyou_t_forum_test; do
      "$PGBIN/psql" -h localhost -p "$PORT" -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 \
        || "$PGBIN/createdb" -h localhost -p "$PORT" -U postgres "$db"
    done
    echo "DATABASE_URL=postgresql://postgres@localhost:$PORT/minyou_t_forum_dev"
    echo "TEST_DATABASE_URL=postgresql://postgres@localhost:$PORT/minyou_t_forum_test"
    ;;
  stop)
    "$PGBIN/pg_ctl" -D "$DATA" stop
    ;;
  *)
    echo "用法：tools/local_db.sh start|stop" >&2
    exit 1
    ;;
esac
