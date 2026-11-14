#!/usr/bin/env bash
# 发卡网一键安装 / 更新脚本
#
# 用法:
#   ./install.sh                      在已解压/已克隆的目录里交互安装
#   ./install.sh --dir /opt/fakawang   指定安装目录(不存在则从仓库下载源码)
#   ./install.sh --update              拉取最新代码并重启(仅 git 安装)
#   ./install.sh --reset-password      重新设置后台密码并重启(不用改文件)
#   ./install.sh --non-interactive     无人值守安装(密码自动生成并打印)
#   ./install.sh --no-proxy            不自动配 Caddy 反代(自己管 Nginx/Caddy 时用)
#
# 安装过程中会直接问你要设置的后台密码和域名,写进 .env,全程不用手动改文件。
set -e

REPO_URL="${REPO_URL:-https://github.com/qwetcn/fakawang.git}"
INSTALL_DIR=""
NON_INTERACTIVE=0
DO_UPDATE=0
DO_RESET_PW=0
NO_PROXY=0
CADDY_DONE=0
NEW_PASSWORD=""
PASSWORD_GENERATED=0
PW_CHANGED=0

while [ $# -gt 0 ]; do
  case "$1" in
    --dir) INSTALL_DIR="$2"; shift 2 ;;
    --non-interactive) NON_INTERACTIVE=1; shift ;;
    --update) DO_UPDATE=1; shift ;;
    --reset-password) DO_RESET_PW=1; shift ;;
    --no-proxy) NO_PROXY=1; shift ;;
    -h|--help)
      echo "用法: $0 [--dir 目录] [--update] [--reset-password] [--non-interactive] [--no-proxy]"
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

# ---------- Caddy 反代 + HTTPS(填了域名才配) ----------
CADDYFILE="${CADDYFILE:-/etc/caddy/Caddyfile}"

install_caddy() {
  # 注意:$SUDO 由 setup_caddy 设置(全局),这里直接用;不能 sudo 整个函数(sudo 调不动 shell 函数)。
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl >/dev/null 2>&1 || true
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | $SUDO gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg 2>/dev/null || return 1
    curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' | $SUDO tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null || return 1
    $SUDO apt-get update >/dev/null 2>&1 && $SUDO apt-get install -y caddy || return 1
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y 'dnf-command(copr)' >/dev/null 2>&1 && $SUDO dnf copr enable -y @caddy/caddy >/dev/null 2>&1 && $SUDO dnf install -y caddy || return 1
  elif command -v yum >/dev/null 2>&1; then
    $SUDO yum install -y yum-plugin-copr >/dev/null 2>&1 && $SUDO yum copr enable -y @caddy/caddy >/dev/null 2>&1 && $SUDO yum install -y caddy || return 1
  else
    return 1
  fi
}

setup_caddy() {
  local site_url="$1" domain=""
  domain="${site_url#https://}"; domain="${domain#http://}"
  domain="${domain%%/*}"; domain="${domain%%:*}"
  case "$domain" in
    *.*) ;;
    *) return 0 ;;
  esac
  case "$domain" in
    [0-9]*.[0-9]*.[0-9]*.[0-9]*)
      say "填的是 IP 地址,跳过 Caddy 配置(HTTPS 证书需要域名)。公网访问请手动配反代,见《部署教程.md》。"
      return 0 ;;
  esac
  SUDO=""
  if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo >/dev/null 2>&1 && sudo -n true 2>/dev/null; then
      SUDO="sudo"
    else
      say "没有 root 权限,跳过 Caddy 自动配置。请按《部署教程.md》手动配反代。"
      return 0
    fi
  fi
  # DNS 提醒:域名必须先解析到这台服务器,证书才申请得下来(只提醒,不中断安装)
  local resolved="" myip=""
  if command -v getent >/dev/null 2>&1; then
    resolved="$(getent hosts "$domain" | awk '{print $1}' | head -n 1)"
  fi
  myip="$(curl -fs --max-time 8 https://ifconfig.me 2>/dev/null || true)"
  if [ -z "$resolved" ]; then
    say "提醒:域名 $domain 暂未解析到任何 IP,请先把 A 记录指到这台服务器($myip),否则 HTTPS 证书申请不到。解析生效后运行: sudo systemctl reload caddy"
  elif [ -n "$myip" ] && [ "$resolved" != "$myip" ]; then
    say "提醒:域名 $domain 现在解析到 $resolved,不是这台服务器($myip)。请把 A 记录改成 $myip,生效后运行: sudo systemctl reload caddy"
  fi
  if ! command -v caddy >/dev/null 2>&1; then
    say "正在安装 Caddy(反代 + 自动 HTTPS)..."
    if ! install_caddy; then
      say "Caddy 安装失败,跳过反代配置。请按《部署教程.md》手动配反代。"
      return 0
    fi
  fi
  $SUDO mkdir -p "$(dirname "$CADDYFILE")" 2>/dev/null || true
  if [ -f "$CADDYFILE" ] && grep -qF "$domain" "$CADDYFILE" 2>/dev/null; then
    say "Caddy 配置里已有 $domain,保持不变。"
  else
    if [ -f "$CADDYFILE" ]; then
      $SUDO cp "$CADDYFILE" "$CADDYFILE.bak.$(date +%Y%m%d%H%M%S)" 2>/dev/null || true
    fi
    printf '\n%s {\n    reverse_proxy 127.0.0.1:8000\n}\n' "$domain" | $SUDO tee -a "$CADDYFILE" >/dev/null
    say "已写入 Caddy 配置:$domain -> 127.0.0.1:8000"
  fi
  if command -v ufw >/dev/null 2>&1 && $SUDO ufw status 2>/dev/null | grep -q "Status: active"; then
    $SUDO ufw allow 80/tcp >/dev/null 2>&1 || true
    $SUDO ufw allow 443/tcp >/dev/null 2>&1 || true
  fi
  if command -v firewall-cmd >/dev/null 2>&1 && $SUDO firewall-cmd --state 2>/dev/null | grep -q running; then
    $SUDO firewall-cmd --permanent --add-service=http --add-service=https >/dev/null 2>&1 || true
    $SUDO firewall-cmd --reload >/dev/null 2>&1 || true
  fi
  if command -v systemctl >/dev/null 2>&1; then
    $SUDO systemctl enable caddy >/dev/null 2>&1 || true
    if $SUDO systemctl is-active --quiet caddy 2>/dev/null; then
      $SUDO systemctl reload caddy 2>/dev/null || $SUDO systemctl restart caddy 2>/dev/null || true
    else
      $SUDO systemctl start caddy 2>/dev/null || true
    fi
  fi
  CADDY_DONE=1
  say "正在验证域名访问(首次申请证书可能需要几十秒)..."
  local n=0
  while [ "$n" -lt 15 ]; do
    if curl -fs --max-time 10 "https://$domain/api/health" >/dev/null 2>&1; then
      say "域名访问正常: https://$domain"
      return 0
    fi
    n=$((n + 1))
    sleep 4
  done
  say "域名暂时还打不开,可能原因:DNS 未生效 / 80,443 端口未放行 / 证书申请中。稍后再试,看日志: journalctl -u caddy --no-pager | tail -30"
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
  if [ "$CADDY_DONE" = "1" ]; then
    say "反代: Caddy 已配置,HTTPS 证书自动申请和续期。"
  elif [ -z "$site" ]; then
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
  for _try in 1 2 3; do
    if [ -n "$EXISTING_URL" ]; then
      read_line "站点域名(当前 $EXISTING_URL,直接回车保留): "
    else
      read_line "站点域名(可选,如 shop.example.com,直接回车跳过): "
    fi
    if [ -z "$REPLY_VAL" ]; then
      break
    fi
    case "$REPLY_VAL" in
      *.*) set_env SITE_URL "$(normalize_url "$REPLY_VAL")"; break ;;
      *) say "域名格式不对,请输入类似 shop.example.com 的域名(或 IP)。" ;;
    esac
  done
fi

# ---------- 启动 ----------
ensure_docker
docker compose up -d --build
wait_ready || die "服务启动超时,请用 docker compose logs 查看日志。"
if [ "$NO_PROXY" = "0" ]; then
  setup_caddy "$(get_env SITE_URL)"
fi
print_summary
