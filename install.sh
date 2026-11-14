#!/usr/bin/env bash
# 发卡网一键安装 / 更新脚本
#
# 用法:
#   ./install.sh                      在已解压/已克隆的目录里交互安装
#   ./install.sh --dir /opt/fakawang   指定安装目录(不存在则从仓库下载源码)
#   ./install.sh --update              拉取最新代码并重启(仅 git 安装)
#   ./install.sh --reset-password      重新设置后台密码并重启(不用改文件)
#   ./install.sh --non-interactive     无人值守安装(密码自动生成并打印)
#
# 安装过程中会直接问你要设置的后台密码和域名,写进 .env,全程不用手动改文件。
set -e

REPO_URL="${REPO_URL:-https://github.com/qwetcn/fakawang.git}"
INSTALL_DIR=""
NON_INTERACTIVE=0
DO_UPDATE=0
DO_RESET_PW=0
NEW_PASSWORD=""
PASSWORD_GENERATED=0
PW_CHANGED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --update) DO_UPDATE=1; shift ;;
    --reset-password) DO_RESET_PW=1; shift ;;
    -h|--help)
      echo "用法: $0 [--dir 目录] [--update] [--reset-password] [--non-interactive]"
      echo "  不带参数:交互安装,过程中直接设置后台密码和域名,不用改文件。"
      exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

say() { printf '%s\n' "$*"; }
die() { printf '错误: %s\n' "$*" >&2; exit 1; }

# ---------- 交互输入:优先 /dev/tty,管道运行一键脚本时也能正常提问 ----------
# /dev/tty 是否真正可用:在子 shell 里实际打开一次(光看权限位会在无控制终端时误判)。
tty_usable() { (exec 9<>/dev/tty) 2>/dev/null; }
read_secret() {
  if tty_usable; then
    exec 9<>/dev/tty
    printf '%s' "$1" >&9
    IFS= read -r -s REPLY_VAL <&9 || REPLY_VAL=""
    printf '\n' >&9
    exec 9>&-
    return 0
  fi
  printf '%s' "$1"
  IFS= read -r -s REPLY_VAL || REPLY_VAL=""
  printf '\n'
}
read_line() {
  if tty_usable; then
    exec 9<>/dev/tty
    printf '%s' "$1" >&9
    IFS= read -r REPLY_VAL <&9 || REPLY_VAL=""
    exec 9>&-
    return 0
  fi
  printf '%s' "$1"
  IFS= read -r REPLY_VAL || REPLY_VAL=""
}

gen_password() {
  if command -v openssl >/dev/null 2>&1; then
    openssl rand -base64 18 | tr -d '/+=\n' | head -c 20
    echo
  else
    tr -dc 'A-Za-z0-9' < /dev/urandom | head -c 20
    echo
  fi
}

ask_password() {
  local p1="" p2="" i=0
  for i in 1 2 3; do
    read_secret "请设置后台管理密码(直接回车则自动生成): "
    p1="$REPLY_VAL"
    if [ -z "$p1" ]; then
      NEW_PASSWORD="$(gen_password)"
      PASSWORD_GENERATED=1
      PW_CHANGED=1
      return 0
    fi
    if [ "${#p1}" -lt 6 ]; then say "密码至少 6 位,请重输。"; continue; fi
    read_secret "请再输一次确认: "
    p2="$REPLY_VAL"
    if [ "$p1" = "$p2" ]; then
      NEW_PASSWORD="$p1"
      PASSWORD_GENERATED=0
      PW_CHANGED=1
      return 0
    fi
    say "两次输入不一致,请重输。"
  done
  die "密码设置失败次数太多,退出。"
}

# ---------- .env 读写 ----------
get_env() { sed -n "s/^$1=//p" .env 2>/dev/null | head -n 1; }
set_env() {
  local key="$1" val="$2"
  touch .env
  if grep -q "^[# ]*$key=" .env; then
    awk -v k="$key" -v v="$val" 'BEGIN{done=0} { if ($0 ~ "^[# ]*"k"=") { print k"="v; done=1 } else { print } } END{ if (!done) print k"="v }' .env > .env.tmp && mv .env.tmp .env
  else
    printf '%s=%s\n' "$key" "$val" >> .env
  fi
  chmod 600 .env 2>/dev/null || true
}
normalize_url() {
  local u="$1"
  u="$(printf '%s' "$u" | tr -d '[:space:]')"
  u="${u%/}"
  case "$u" in
    http://*|https://*) printf '%s' "$u" ;;
    *) printf 'https://%s' "$u" ;;
  esac
}

# ---------- Docker ----------
ensure_docker() {
  if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    docker compose version >/dev/null 2>&1 || die "未检测到 docker compose 插件(v2),请安装 docker-compose-plugin 后重试。"
    return 0
  fi
  if ! command -v docker >/dev/null 2>&1; then
    say "未检测到 Docker,正在安装(官方脚本,可能需要几分钟)..."
    curl -fsSL https://get.docker.com | sh || die "Docker 安装失败,请手动安装后重试: https://docs.docker.com/engine/install/"
    if command -v systemctl >/dev/null 2>&1; then systemctl enable --now docker >/dev/null 2>&1 || true; fi
  fi
  docker info >/dev/null 2>&1 || die "Docker 服务未启动或当前用户无权访问,请用 root 或 sudo 运行。"
  docker compose version >/dev/null 2>&1 || die "未检测到 docker compose 插件(v2),请安装 docker-compose-plugin 后重试。"
}

wait_ready() {
  local n=0
  say "正在启动,等待服务就绪..."
  while [ "$n" -lt 30 ]; do
    if curl -fs -o /dev/null http://127.0.0.1:8000/api/health 2>/dev/null; then
      return 0
    fi
    n=$((n + 1))
    sleep 2
  done
  return 1
}

print_summary() {
  local site shop_url admin_url
  site="$(get_env SITE_URL)"
  if [ -n "$site" ]; then
    shop_url="$site"; admin_url="$site/admin"
  else
    shop_url="http://127.0.0.1:8000 (仅本机)"; admin_url="http://127.0.0.1:8000/admin (仅本机)"
  fi
  say "=================================="
  say "完成!"
  say "店铺地址: $shop_url"
  say "后台地址: $admin_url"
  if [ "$PASSWORD_GENERATED" = "1" ]; then
    say "后台密码: $NEW_PASSWORD   (自动生成,请妥善保存)"
  elif [ "$PW_CHANGED" = "1" ]; then
    say "后台密码: 已设置为你刚才输入的密码"
  else
    say "后台密码: 与之前设置的相同"
  fi
  if [ -z "$site" ]; then
    say "提示: 公网访问需配置反代 + 域名,见《部署教程.md》「域名与 HTTPS」一节。"
  fi
  say "查看日志: cd $INSTALL_DIR && docker compose logs -f"
  say "更新版本: cd $INSTALL_DIR && ./install.sh --update"
  say "修改密码: cd $INSTALL_DIR && ./install.sh --reset-password"
  say "=================================="
}

# ---------- 确定安装目录与源码 ----------
if [ -f "./docker-compose.yml" ] && [ -f "./app.py" ]; then
  INSTALL_DIR="$(pwd)"
elif [ -z "$INSTALL_DIR" ]; then
  INSTALL_DIR="/opt/fakawang"
fi

if [ ! -f "$INSTALL_DIR/docker-compose.yml" ]; then
  [ -n "$REPO_URL" ] || die "未找到本地源码且未设置仓库地址(REPO_URL),请在解压后的目录里运行 ./install.sh。"
  command -v git >/dev/null 2>&1 || die "需要 git,请先安装 git。"
  if [ -d "$INSTALL_DIR" ] && [ -n "$(ls -A "$INSTALL_DIR" 2>/dev/null)" ]; then
    die "目录 $INSTALL_DIR 已存在且非空,又没有店铺文件,请换个目录(--dir)或清空后重试。"
  fi
  say "正在下载源码到 $INSTALL_DIR ..."
  git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
elif [ "$DO_UPDATE" = "1" ]; then
  if [ -d "$INSTALL_DIR/.git" ]; then
    say "正在拉取最新代码..."
    git -C "$INSTALL_DIR" pull --ff-only || die "git pull 失败,请检查网络或本地修改。"
  else
    say "当前不是 git 安装,跳过拉取,直接重新构建启动。"
  fi
fi
cd "$INSTALL_DIR"

# ---------- 配置(.env 全由脚本写,不用手动改文件) ----------
[ -f .env ] || cp .env.example .env

if [ "$DO_RESET_PW" = "1" ]; then
  if [ "$NON_INTERACTIVE" = "1" ]; then
    NEW_PASSWORD="$(gen_password)"; PASSWORD_GENERATED=1; PW_CHANGED=1
  else
    ask_password
  fi
  set_env ADMIN_PASSWORD "$NEW_PASSWORD"
  ensure_docker
  docker compose up -d --build
  wait_ready || say "服务启动超时,请用 docker compose logs 查看日志。"
  print_summary
  exit 0
fi

EXISTING_PW="$(get_env ADMIN_PASSWORD)"
if [ -n "$EXISTING_PW" ]; then
  say "检测到已有配置,后台密码保持不变。"
  if [ "$NON_INTERACTIVE" = "0" ]; then
    read_line "是否重新设置后台密码? [y/N] "
    case "$REPLY_VAL" in
      y|Y|yes|YES) ask_password; set_env ADMIN_PASSWORD "$NEW_PASSWORD" ;;
    esac
  fi
else
  if [ "$NON_INTERACTIVE" = "1" ]; then
    NEW_PASSWORD="$(gen_password)"; PASSWORD_GENERATED=1; PW_CHANGED=1
  else
    ask_password
  fi
  set_env ADMIN_PASSWORD "$NEW_PASSWORD"
fi

EXISTING_URL="$(get_env SITE_URL)"
if [ "$NON_INTERACTIVE" = "0" ]; then
  if [ -n "$EXISTING_URL" ]; then
    read_line "站点域名(当前 $EXISTING_URL,直接回车保留): "
  else
    read_line "站点域名(可选,如 shop.example.com,直接回车跳过): "
  fi
  if [ -n "$REPLY_VAL" ]; then
    set_env SITE_URL "$(normalize_url "$REPLY_VAL")"
  fi
fi

# ---------- 启动 ----------
ensure_docker
docker compose up -d --build
wait_ready || die "服务启动超时,请用 docker compose logs 查看日志。"
print_summary
