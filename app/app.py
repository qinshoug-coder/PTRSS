import json
import os
import re
import secrets
import threading
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from time import monotonic, sleep, strftime
from urllib.parse import parse_qs, urljoin, urlparse
from xml.etree import ElementTree as ET

import requests
from flask import Flask, jsonify, redirect, render_template_string, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

DATA_FILE = Path('/data/config.json')
STATE_FILE = Path('/data/state.json')
LOG_FILE = Path('/data/ptrss.log')
TG_OFFSET_FILE = Path('/data/tg_update_offset.txt')
AUTH_FILE = Path('/data/web_auth.json')
SESSION_SECRET_FILE = Path('/data/web_session_secret.txt')
PORT = int(os.getenv('PTRSS_WEB_PORT', '7790'))
PUBLIC_BASE_URL = os.getenv('PTRSS_PUBLIC_BASE_URL', f'http://127.0.0.1:{PORT}')

app = Flask(__name__)
state_lock = threading.Lock()
poll_lock = threading.Lock()
config_lock = threading.Lock()


def ensure_session_secret():
    if SESSION_SECRET_FILE.exists():
        secret = SESSION_SECRET_FILE.read_text(encoding='utf-8').strip()
        if secret:
            return secret
    secret = secrets.token_urlsafe(48)
    SESSION_SECRET_FILE.write_text(secret, encoding='utf-8')
    return secret


app.secret_key = ensure_session_secret()
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_NAME'] = 'ptrss_session'
app.config['PERMANENT_SESSION_LIFETIME'] = 60 * 60 * 12
if PUBLIC_BASE_URL.startswith('https://'):
    app.config['SESSION_COOKIE_SECURE'] = True


def ensure_web_auth():
    if AUTH_FILE.exists():
        try:
            data = json.loads(AUTH_FILE.read_text(encoding='utf-8'))
            if str(data.get('username') or '').strip() and str(data.get('password_hash') or '').strip():
                return data
        except Exception:
            pass
    data = {
        'username': os.getenv('PTRSS_WEB_USERNAME', 'admin'),
        'password_hash': generate_password_hash(os.getenv('PTRSS_WEB_PASSWORD', 'change-me')),

    }
    AUTH_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    return data


def get_web_auth():
    return ensure_web_auth()


def is_logged_in():
    return bool(session.get('ptrss_authed'))


def wants_json_response():
    accept = (request.headers.get('Accept') or '').lower()
    requested_with = (request.headers.get('X-Requested-With') or '').lower()
    return 'application/json' in accept or requested_with == 'xmlhttprequest'


def json_or_redirect(message, ok=True, redirect_endpoint='config_page', redirect_values=None, **payload):
    if wants_json_response():
        body = {'ok': bool(ok), 'message': message}
        body.update(payload)
        return jsonify(body), (200 if ok else 400)
    params = {'msg': message}
    if redirect_values:
        params.update(redirect_values)
    return redirect(url_for(redirect_endpoint, **params))

DEFAULT_CONFIG = {
    'base': {
        'timezone': 'Asia/Shanghai',
        'web_port': 7790,
        'poll_seconds': 300,
        'push_enabled': True,
    },
    'telegram': {
        'bot_token': '',
        'chat_id': '',
        'enabled': False,
    },
    'sites': [],
    'downloaders': [],
}

DEFAULT_STATE = {
    'sites': {},
    'entry_map': {},
    'pending_upload_limits': {},
    'meta': {
        'last_poll_at': '',
        'last_poll_summary': '',
    },
}

BASE_HEAD = """
<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>PTRSS</title>
  <style>
    *{box-sizing:border-box}
    body{margin:0;font-family:Arial,sans-serif;background:#0b0f14;color:#e6edf3}
    .wrap{max-width:1480px;margin:0 auto;padding:24px}
    h1,h2,h3{margin:0 0 14px}
    .muted{color:#8b98a5}
    .grid{display:grid;grid-template-columns:1.12fr 1fr;gap:18px;align-items:start}
    .stack{display:grid;gap:18px}
    .card{background:#111821;border:1px solid #263041;border-radius:14px;padding:18px;box-shadow:0 6px 24px rgba(0,0,0,.18)}
    .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}
    label{display:block;font-size:13px;color:#9fb0c2;margin:0 0 6px}
    input,textarea,select{width:100%;background:#0f141b;color:#e6edf3;border:1px solid #2a3443;border-radius:10px;padding:10px 12px;outline:none}
    textarea{min-height:88px;resize:vertical}
    input[type=checkbox]{width:auto;transform:translateY(1px)}
    .check{display:flex;gap:8px;align-items:center;margin:8px 0}
    button,.button{display:inline-flex;align-items:center;justify-content:center;background:#2f81f7;color:#fff;border:none;border-radius:10px;padding:10px 14px;cursor:pointer;text-decoration:none;font-size:14px}
    button.secondary,.button.secondary{background:#1b2330;color:#d9e2ec;border:1px solid #334155}
    button.danger{background:#8b1e1e}
    table{width:100%;border-collapse:collapse}
    th,td{padding:10px 8px;border-bottom:1px solid #243041;vertical-align:top;text-align:left;font-size:14px}
    th{color:#9fb0c2;font-weight:600}
    .badge{display:inline-block;padding:4px 8px;border-radius:999px;background:#16324f;color:#9dd1ff;font-size:12px}
    .ok{background:#153b2a;color:#98f1b5}
    .warn{background:#433114;color:#ffd685}
    .topbar{display:flex;justify-content:space-between;gap:12px;align-items:end;margin-bottom:18px}
    .msg{margin:0 0 16px;background:#15324a;border:1px solid #275272;color:#b8e0ff;padding:12px 14px;border-radius:10px}
    .msg.error{background:#4a1f24;border-color:#7a2d37;color:#ffd5db}
    .toast{position:fixed;right:18px;bottom:18px;z-index:10001;min-width:220px;max-width:min(520px,calc(100vw - 36px));background:#15324a;border:1px solid #275272;color:#b8e0ff;padding:12px 14px;border-radius:12px;box-shadow:0 10px 30px rgba(0,0,0,.32);opacity:0;transform:translateY(10px);pointer-events:none;transition:all .18s ease}
    .toast.show{opacity:1;transform:translateY(0)}
    .toast.error{background:#4a1f24;border-color:#7a2d37;color:#ffd5db}
    .small{font-size:12px}
    code{background:#0f141b;padding:2px 6px;border-radius:6px}
    .actions{display:flex;gap:8px;flex-wrap:wrap}
    .actions form{margin:0}
    .mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;word-break:break-all}
    pre{background:#0f141b;border:1px solid #243041;border-radius:10px;padding:12px;max-height:72vh;overflow:auto;white-space:pre-wrap;word-break:break-word}
    .toolbar{display:flex;gap:8px;justify-content:flex-end;align-items:center;flex-wrap:wrap}
    .subtle{font-size:12px;color:#9fb0c2;margin-top:8px}
    .nav{display:flex;gap:10px;flex-wrap:wrap;margin:14px 0 20px}
    .nav a{display:inline-flex;align-items:center;justify-content:center;padding:10px 14px;border-radius:10px;text-decoration:none;border:1px solid #334155;background:#111821;color:#d9e2ec}
    .nav a.active{background:#2f81f7;border-color:#2f81f7;color:#fff}
    .feed-head{display:grid;gap:8px;margin-bottom:16px}
    .feed-list{display:grid;gap:12px}
    .feed-item{border:1px solid #243041;border-radius:12px;padding:14px;background:#0f141b}
    .feed-item h3{margin-bottom:8px;font-size:16px;line-height:1.4}
    .feed-meta{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px}
    .feed-item a{color:#78b7ff;text-decoration:none}
    .feed-item p{margin:10px 0 0;color:#c1ccd8;line-height:1.55}
    .empty{padding:20px;border:1px dashed #334155;border-radius:12px;color:#9fb0c2}
    @media (max-width: 980px){
      .grid,.row{grid-template-columns:1fr}
      .wrap{padding:14px}
      .toolbar{justify-content:flex-start}
    }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div>
        <h1>PTRSS</h1>
        <div class="muted">RSS 站点、下载器、轮询状态</div>
      </div>
      <div class="small muted">简洁模式</div>
    </div>

    {% if message %}
    <div class="msg">{{ message }}</div>
    {% endif %}

    {% if active_tab != 'login' %}
    <div class="nav">
      <a href="/logs" class="{{ 'active' if active_tab == 'logs' else '' }}">日志</a>
      <a href="/config" class="{{ 'active' if active_tab == 'config' else '' }}">配置</a>
      <a href="/logout">退出</a>
    </div>
    {% endif %}
    <div id="toast" class="toast"></div>
"""

LOGIN_PAGE = BASE_HEAD + """
    <style>
      .login-shell{min-height:70vh;display:grid;place-items:center}
      .login-card{width:min(420px,100%);background:#111821;border:1px solid #263041;border-radius:16px;padding:22px;box-shadow:0 10px 30px rgba(0,0,0,.28)}
      .login-card h2{margin-bottom:8px}
      .login-card .muted{margin-bottom:16px}
    </style>
    <div class="login-shell">
      <div class="login-card">
        <h2>PTRSS 登录</h2>
        <div class="muted">公网访问已启用登录保护</div>
        <form method="post" action="/login">
          <label>账号</label>
          <input name="username" autocomplete="username" autofocus>
          <label>密码</label>
          <input name="password" type="password" autocomplete="current-password">
          <div class="sticky-actions"><button type="submit">登录</button></div>
        </form>
      </div>
    </div>
  </div>
</body>
</html>
"""

CONFIG_PAGE = BASE_HEAD + """
    <style>
      .hero-grid{display:grid;grid-template-columns:1fr;gap:14px;margin-bottom:14px}
      .mini-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px}
      .stat{background:#0f141b;border:1px solid #243041;border-radius:12px;padding:10px}
      .stat .k{font-size:11px;color:#8b98a5;margin-bottom:3px}
      .stat .v{font-size:17px;font-weight:700}
      .section-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:8px}
      .section-title h2{margin:0;font-size:18px}
      .list-grid{display:grid;gap:8px}
      .item-card{background:#0f141b;border:1px solid #243041;border-radius:12px;padding:10px}
      .item-head{display:flex;justify-content:space-between;gap:10px;align-items:start;margin-bottom:6px}
      .item-title{font-size:14px;font-weight:700}
      .item-lines{display:grid;gap:3px}
      .item-line{font-size:12px;color:#c1ccd8}
      .item-line .label{color:#8b98a5;margin-right:6px}
      .split-2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
      .hint{font-size:11px;color:#8b98a5;line-height:1.5}
      .tight textarea{min-height:56px}
      .tight input,.tight textarea,.tight select{margin-bottom:2px}
      .compact-grid{display:grid;grid-template-columns:1.1fr .9fr;gap:14px}
      .muted-box{padding:10px 12px;border:1px solid #243041;border-radius:10px;background:#0f141b;color:#aab7c5;font-size:12px}
      .sticky-actions{position:sticky;bottom:12px;display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;margin-top:12px}
      .modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.65);display:flex;align-items:center;justify-content:center;padding:20px;z-index:9999}
      .modal{width:min(860px,100%);max-height:90vh;overflow:auto;background:#111821;border:1px solid #263041;border-radius:16px;padding:18px;box-shadow:0 20px 60px rgba(0,0,0,.45)}
      .modal .sticky-actions{position:sticky;background:#111821;padding-top:12px}
      @media (max-width: 980px){
        .hero-grid,.split-2,.mini-grid,.compact-grid{grid-template-columns:1fr}
      }
    </style>

    <div class="hero-grid">
      <div class="card">
        <div class="section-title">
          <h2>总览</h2>
          <div class="actions">
            <a class="button secondary" href="/logs">日志</a>
            <form method="post" action="/run-poll"><button type="submit">立即轮询</button></form>
          </div>
        </div>
        <div class="mini-grid">
          <div class="stat"><div class="k">站点</div><div class="v">{{ cfg.sites|length }}</div></div>
          <div class="stat"><div class="k">下载器</div><div class="v">{{ cfg.downloaders|length }}</div></div>
          <div class="stat"><div class="k">Telegram</div><div class="v">{{ '开' if cfg.telegram.enabled else '关' }}</div></div>
          <div class="stat"><div class="k">推送</div><div class="v">{{ '开' if cfg.base.push_enabled else '关' }}</div></div>
        </div>
      </div>
    </div>

    <div class="split-2">
      <div class="stack">
        <div class="card tight">
          <div class="section-title"><h2>基础 / Telegram</h2></div>
          <div class="compact-grid">
            <form method="post" action="/save-base" data-async="true">
              <div class="row">
                <div><label>时区</label><input name="timezone" value="{{ cfg.base.timezone }}" placeholder="Asia/Shanghai"></div>
                <div><label>轮询秒数</label><input name="poll_seconds" value="{{ cfg.base.poll_seconds }}" placeholder="300"></div>
              </div>
              <div class="row">
                <div><label>Web 端口</label><input value="{{ cfg.base.web_port }}" disabled></div>
                <div><label>最近轮询</label><input value="{{ meta.last_poll_at or '暂无' }}" disabled></div>
              </div>
              <div class="sticky-actions"><button type="submit">保存基础</button></div>
            </form>
            <form method="post" action="/save-telegram" data-async="true">
              <label>Bot Token</label>
              <textarea name="bot_token" placeholder="贴 Bot Token">{{ cfg.telegram.bot_token }}</textarea>
              <label>Chat ID</label>
              <input name="chat_id" value="{{ cfg.telegram.chat_id }}" placeholder="例如 8499032032">
              <label class="check"><input type="checkbox" name="enabled" {% if cfg.telegram.enabled %}checked{% endif %}> 启用 Telegram</label>
              <div class="sticky-actions">
                <button class="secondary" type="submit" formaction="/test-telegram">测试 TG</button>
                <button type="submit">保存 TG</button>
              </div>
            </form>
          </div>
        </div>

        <div class="card">
          <div class="section-title">
            <h2>站点列表</h2>
            <div class="actions"><a class="button" href="/site/new">新增站点</a></div>
          </div>
          <div class="list-grid">
            {% for site in cfg.sites %}
            {% set st = site_states.get(loop.index0, {}) %}
            <div class="item-card">
              <div class="item-head">
                <div>
                  <div class="item-title">{{ site.name or '未命名站点' }}</div>
                  <div class="muted small mono">{{ site.base_url or '' }}</div>
                </div>
                <span class="badge {% if site.enabled %}ok{% else %}warn{% endif %}">{{ '启用' if site.enabled else '停用' }}</span>
              </div>
              <div class="item-lines">
                <div class="item-line"><span class="label">分集</span>{{ '开' if site.push_episodes else '关' }}</div>
                <div class="item-line"><span class="label">成功</span>{{ st.last_seen_at or '暂无' }}</div>
                <div class="item-line"><span class="label">异常</span>{{ st.last_error or '无' }}</div>
                <div class="item-line"><span class="label">记忆</span>{{ st.seen_count or 0 }} 条</div>
              </div>
              <div class="actions" style="margin-top:10px">
                <a class="button secondary" href="/site/{{ loop.index0 }}/edit">编辑</a>
                <form method="post" action="/preview-site"><input type="hidden" name="index" value="{{ loop.index0 }}"><button class="secondary" type="submit">预览</button></form>
                <form method="post" action="/test-site"><input type="hidden" name="index" value="{{ loop.index0 }}"><button class="secondary" type="submit">测试</button></form>
                <form method="post" action="/delete-site" onsubmit="return confirm('确认删除该站点？')"><input type="hidden" name="index" value="{{ loop.index0 }}"><button class="danger" type="submit">删</button></form>
              </div>
            </div>
            {% else %}
            <div class="empty">还没有站点。先把 RSS 链接加进来。</div>
            {% endfor %}
          </div>
        </div>
      </div>

      <div class="stack">
        <div class="card">
          <div class="section-title">
            <h2>下载器列表</h2>
            <div class="actions"><a class="button" href="/downloader/new">新增下载器</a></div>
          </div>
          <div class="list-grid">
            {% for dl in cfg.downloaders %}
            <div class="item-card">
              <div class="item-head">
                <div>
                  <div class="item-title">{{ dl.name or '未命名下载器' }}</div>
                  <div class="muted small">{{ dl.type or 'qbittorrent' }} / {{ dl.username or '' }}</div>
                </div>
                <span class="badge {% if dl.enabled %}ok{% else %}warn{% endif %}">{{ '启用' if dl.enabled else '停用' }}</span>
              </div>
              <div class="item-lines">
                <div class="item-line"><span class="label">地址</span><span class="mono">{{ dl.url }}</span></div>
                {% if dl.category %}<div class="item-line"><span class="label">分类</span>{{ dl.category }}</div>{% endif %}
                {% if dl.tags %}<div class="item-line"><span class="label">标签</span>{{ dl.tags }}</div>{% endif %}
                {% if dl.savepath %}<div class="item-line"><span class="label">保存路径</span><span class="mono">{{ dl.savepath }}</span></div>{% endif %}
              </div>
              <div class="actions" style="margin-top:10px">
                <a class="button secondary" href="/downloader/{{ loop.index0 }}/edit">编辑</a>
                <form method="post" action="/test-downloader"><input type="hidden" name="index" value="{{ loop.index0 }}"><button class="secondary" type="submit">测试</button></form>
                <form method="post" action="/delete-downloader" onsubmit="return confirm('确认删除该下载器？')"><input type="hidden" name="index" value="{{ loop.index0 }}"><button class="danger" type="submit">删</button></form>
              </div>
            </div>
            {% else %}
            <div class="empty">还没有下载器。先把 qBittorrent 连上。</div>
            {% endfor %}
          </div>
        </div>

        <div class="card">
          <div class="section-title"><h2>当前状态</h2></div>
          <div class="muted-box">
            <div>最近轮询：{{ meta.last_poll_at or '暂无' }}</div>
            <div style="margin-top:6px">轮询摘要：{{ meta.last_poll_summary or '暂无' }}</div>
          </div>
        </div>
      </div>
    </div>

  </div>
</body>
</html>
"""

EDIT_SITE_PAGE = BASE_HEAD + """
    <div class="stack">
      <div class="card tight">
        <div class="section-title">
          <h2>{{ '编辑站点' if is_edit else '新增站点' }}</h2>
          <div class="actions">
            <a class="button secondary" href="/config">返回</a>
          </div>
        </div>
        <form method="post" action="/save-site">
          <input type="hidden" name="edit_index" value="{{ edit_site_idx if edit_site_idx >= 0 else '' }}">
          <div class="row">
            <div><label>站点名称</label><input name="name" value="{{ site.name if site else '' }}" placeholder="例如 HDSky / OurBits"></div>
            <div><label>基础域名</label><input name="base_url" value="{{ site.base_url if site else '' }}" placeholder="例如 https://hdsky.me"></div>
          </div>
          <label>RSS 链接</label>
          <textarea name="rss_url" placeholder="贴完整 RSS 链接">{{ site.rss_url if site else '' }}</textarea>
          <label>Cookie</label>
          <textarea name="cookie" placeholder="能不填就先别填；需要详情页/下载时再补">{{ site.cookie if site else '' }}</textarea>
          <label class="check"><input type="checkbox" name="enabled" {% if not site or site.enabled %}checked{% endif %}> 启用该站点</label>
          <label class="check"><input type="checkbox" name="push_episodes" {% if not site or site.push_episodes %}checked{% endif %}> 推送分集资源</label>
          <div class="hint">Cookie 留空时保留旧值。</div>
          <div class="sticky-actions">
            {% if is_edit %}
            <button class="secondary" type="submit" formaction="/test-site" name="index" value="{{ edit_site_idx }}">测试</button>
            <button class="secondary" type="submit" formaction="/preview-site" name="index" value="{{ edit_site_idx }}">预览</button>
            {% endif %}
            <a class="button secondary" href="/config">取消</a>
            <button type="submit">{{ '保存站点修改' if is_edit else '新增站点' }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</body>
</html>
"""

EDIT_DOWNLOADER_PAGE = BASE_HEAD + """
    <div class="stack">
      <div class="card tight">
        <div class="section-title">
          <h2>{{ '编辑下载器' if is_edit else '新增下载器' }}</h2>
          <div class="actions">
            <a class="button secondary" href="/config">返回</a>
          </div>
        </div>
        <form method="post" action="/save-downloader">
          <input type="hidden" name="edit_index" value="{{ edit_dl_idx if edit_dl_idx >= 0 else '' }}">
          <div class="row">
            <div><label>下载器名称</label><input name="name" value="{{ dl.name if dl else '' }}" placeholder="例如 白群晖220+"></div>
            <div><label>类型</label><select name="type"><option value="qbittorrent" {% if not dl or dl.type == 'qbittorrent' %}selected{% endif %}>qBittorrent</option></select></div>
          </div>
          <label>地址</label>
          <input name="url" value="{{ dl.url if dl else '' }}" placeholder="例如 http://127.0.0.1:8080">
          <div class="row">
            <div><label>用户名</label><input name="username" value="{{ dl.username if dl else '' }}"></div>
            <div><label>密码</label><input name="password" type="password" value="{{ dl.password if dl else '' }}"></div>
          </div>
          <div class="row">
            <div><label>分类</label><input name="category" value="{{ dl.category if dl else '' }}"></div>
            <div><label>标签</label><input name="tags" value="{{ dl.tags if dl else '' }}"></div>
          </div>
          <label>保存路径</label>
          <input name="savepath" value="{{ dl.savepath if dl else '' }}" placeholder="例如 /downloads/PT">
          <label class="check"><input type="checkbox" name="enabled" {% if not dl or dl.enabled %}checked{% endif %}> 启用该下载器</label>
          <div class="hint">密码留空时保留旧值。</div>
          <div class="sticky-actions">
            {% if is_edit %}
            <button class="secondary" type="submit" formaction="/test-downloader" name="index" value="{{ edit_dl_idx }}">测试</button>
            {% endif %}
            <a class="button secondary" href="/config">取消</a>
            <button type="submit">{{ '保存下载器修改' if is_edit else '新增下载器' }}</button>
          </div>
        </form>
      </div>
    </div>
  </div>
</body>
</html>
"""

LOGS_PAGE = BASE_HEAD + """
    <div class="stack">
      <div class="card">
        <div class="toolbar">
          <a class="button secondary" href="/logs">刷新</a>
          <a class="button secondary" href="/logs?lines=300">近 300 行</a>
          <a class="button secondary" href="/logs?lines=1000">近 1000 行</a>
          <a class="button secondary" href="/logs/raw?lines={{ lines }}" target="_blank">纯文本</a>
          <a class="button secondary" href="/config">去配置页</a>
        </div>
        <h2>运行日志</h2>
        <div class="muted small" style="margin-bottom:10px">当前展示最近 {{ lines }} 行。内嵌浏览器卡顿时，优先点“纯文本”。</div>
        <pre>{{ logs }}</pre>
      </div>
    </div>
  </div>
</body>
</html>
"""
PREVIEW_PAGE = BASE_HEAD + """
    <div class="stack">
      <div class="card">
        <div class="toolbar">
          <span class="badge">RSS 实拉预览</span>
          <a class="button secondary" href="/config">返回配置页</a>
        </div>
        <div class="feed-head">
          <h2>{{ site.name or '未命名站点' }}</h2>
          <div class="muted mono">{{ site.rss_url }}</div>
          <div class="actions">
            <span class="badge {% if preview.ok %}ok{% else %}warn{% endif %}">{{ '拉取成功' if preview.ok else '拉取失败' }}</span>
            <span class="badge">HTTP {{ preview.status_code }}</span>
            <span class="badge">条目 {{ preview.item_count }}</span>
            <span class="badge">格式 {{ preview.feed_type or 'unknown' }}</span>
          </div>
          {% if preview.feed_title %}<div class="muted">Feed 标题：{{ preview.feed_title }}</div>{% endif %}
          {% if preview.error %}<div class="muted">错误：{{ preview.error }}</div>{% endif %}
        </div>
      </div>

      <div class="card">
        <h2>原始响应预览</h2>
        <pre>{{ preview.raw_preview }}</pre>
      </div>

      <div class="card">
        <h2>解析到的条目</h2>
        {% if preview.items %}
        <div class="feed-list">
          {% for item in preview.items %}
          <div class="feed-item">
            <h3>{{ item.title }}</h3>
            <div class="feed-meta">
              {% if item.published %}<span class="badge">{{ item.published }}</span>{% endif %}
              {% if item.guid %}<span class="badge mono">GUID: {{ item.guid }}</span>{% endif %}
            </div>
            {% if item.link %}<div><a href="{{ item.link }}" target="_blank" rel="noreferrer">{{ item.link }}</a></div>{% endif %}
            {% if item.enclosure %}<div class="muted small mono" style="margin-top:8px">Enclosure: {{ item.enclosure }}</div>{% endif %}
            {% if item.description %}<p>{{ item.description }}</p>{% endif %}
          </div>
          {% endfor %}
        </div>
        {% else %}
        <div class="empty">这次没有解析出条目。常见原因：返回的不是 RSS/XML、Cookie 失效、被站点重定向到登录页、或者源站输出格式很怪。</div>
        {% endif %}
      </div>
    </div>
  </div>
</body>
</html>
"""


@app.before_request
def require_login_for_web():
    allowed = {'login_page', 'login_submit', 'logout', 'static'}
    if request.endpoint in allowed:
        return None
    if request.path.startswith('/logs/raw'):
        pass
    if request.path.startswith('/telegram'):
        return None
    if request.path.startswith('/favicon'):
        return None
    if is_logged_in():
        return None
    if wants_json_response():
        return jsonify({'ok': False, 'message': '未登录'}), 401
    return redirect(url_for('login_page', next=request.path))


@app.get('/login')
def login_page():
    if is_logged_in():
        return redirect(url_for('config_page'))
    return render_template_string(LOGIN_PAGE, active_tab='login', message=request.args.get('msg', '').strip())


@app.post('/login')
def login_submit():
    auth = get_web_auth()
    username = (request.form.get('username') or '').strip()
    password = request.form.get('password') or ''
    if username == auth.get('username') and check_password_hash(auth.get('password_hash') or '', password):
        session['ptrss_authed'] = True
        session.permanent = True
        return redirect(url_for('config_page'))
    return redirect(url_for('login_page', msg='账号或密码错误'))


@app.get('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page', msg='已退出登录'))


def deep_copy_default():
    return json.loads(json.dumps(DEFAULT_CONFIG))


def deep_copy_state_default():
    return json.loads(json.dumps(DEFAULT_STATE))


def now_text():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def log(*parts):
    line = f'[{strftime("%Y-%m-%d %H:%M:%S")}] ' + ' '.join(str(p) for p in parts)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def read_logs(lines=120):
    if not LOG_FILE.exists():
        return '暂无日志'
    try:
        data = LOG_FILE.read_text(encoding='utf-8', errors='ignore').splitlines()
        return '\n'.join(data[-lines:]) or '暂无日志'
    except Exception as exc:
        return f'读取日志失败：{exc}'


def ensure_config_shape(cfg):
    base = cfg.get('base') if isinstance(cfg.get('base'), dict) else {}
    telegram = cfg.get('telegram') if isinstance(cfg.get('telegram'), dict) else {}
    sites = cfg.get('sites') if isinstance(cfg.get('sites'), list) else []
    downloaders = cfg.get('downloaders') if isinstance(cfg.get('downloaders'), list) else []

    merged = deep_copy_default()
    merged['base'].update(base)
    merged['base']['push_enabled'] = bool(merged['base'].get('push_enabled', True))
    merged['telegram'].update(telegram)
    merged['sites'] = [normalize_site(item) for item in sites if isinstance(item, dict)]
    merged['downloaders'] = [normalize_downloader(item) for item in downloaders if isinstance(item, dict)]
    return merged


def normalize_site(item):
    rss_url = str(item.get('rss_url') or '').strip()
    base_url = str(item.get('base_url') or '').strip()
    if not base_url and rss_url:
        parsed = urlparse(rss_url)
        if parsed.scheme and parsed.netloc:
            base_url = f'{parsed.scheme}://{parsed.netloc}'
    push_episodes = item.get('push_episodes')
    if push_episodes is None:
        push_episodes = True
    return {
        'name': str(item.get('name') or '').strip(),
        'base_url': base_url,
        'rss_url': rss_url,
        'cookie': str(item.get('cookie') or '').strip(),
        'enabled': bool(item.get('enabled')),
        'push_episodes': bool(push_episodes),
    }


def normalize_downloader(item):
    return {
        'name': str(item.get('name') or '').strip(),
        'type': str(item.get('type') or 'qbittorrent').strip() or 'qbittorrent',
        'url': str(item.get('url') or '').strip(),
        'username': str(item.get('username') or '').strip(),
        'password': str(item.get('password') or '').strip(),
        'category': str(item.get('category') or '').strip(),
        'tags': str(item.get('tags') or '').strip(),
        'savepath': str(item.get('savepath') or '').strip(),
        'enabled': bool(item.get('enabled')),
    }


def load_config():
    with config_lock:
        if not DATA_FILE.exists():
            cfg = deep_copy_default()
            save_config(cfg)
            log('config missing; initialized default config')
            return cfg

        try:
            raw_text = DATA_FILE.read_text(encoding='utf-8')
            raw = json.loads(raw_text)
        except Exception as exc:
            backup_file = DATA_FILE.with_suffix('.json.bak')
            if backup_file.exists():
                try:
                    backup_raw = json.loads(backup_file.read_text(encoding='utf-8'))
                    cfg = ensure_config_shape(backup_raw)
                    save_config(cfg)
                    log('config load failed; restored from backup', short_error(exc))
                    return cfg
                except Exception as backup_exc:
                    log('config backup restore failed', short_error(backup_exc))
            log('config load failed; keep broken file untouched', short_error(exc))
            raise

        cfg = ensure_config_shape(raw)
        return cfg


def save_config(cfg):
    shaped = ensure_config_shape(cfg)
    with config_lock:
        DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
        backup_file = DATA_FILE.with_suffix('.json.bak')
        tmp_file = DATA_FILE.with_suffix('.json.tmp')
        if DATA_FILE.exists():
            try:
                backup_file.write_text(DATA_FILE.read_text(encoding='utf-8'), encoding='utf-8')
            except Exception as exc:
                log('config backup write failed', short_error(exc))
        tmp_file.write_text(json.dumps(shaped, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp_file.replace(DATA_FILE)


def ensure_state_shape(state):
    if not isinstance(state, dict):
        state = {}
    sites = state.get('sites') if isinstance(state.get('sites'), dict) else {}
    entry_map = state.get('entry_map') if isinstance(state.get('entry_map'), dict) else {}
    pending_upload_limits = state.get('pending_upload_limits') if isinstance(state.get('pending_upload_limits'), dict) else {}
    meta = state.get('meta') if isinstance(state.get('meta'), dict) else {}
    merged = deep_copy_state_default()
    merged['sites'] = sites
    merged['entry_map'] = entry_map
    merged['pending_upload_limits'] = pending_upload_limits
    merged['meta'].update(meta)
    return merged


def load_state():
    with state_lock:
        if not STATE_FILE.exists():
            state = deep_copy_state_default()
            save_state(state)
            return state
        try:
            raw = json.loads(STATE_FILE.read_text(encoding='utf-8'))
        except Exception:
            raw = deep_copy_state_default()
        state = ensure_state_shape(raw)
        save_state(state)
        return state


def save_state(state):
    shaped = ensure_state_shape(state)
    entry_map = shaped.get('entry_map', {})
    if len(entry_map) > 500:
        keys = list(entry_map.keys())[-500:]
        shaped['entry_map'] = {k: entry_map[k] for k in keys}
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(shaped, ensure_ascii=False, indent=2), encoding='utf-8')


def state_key_for_site(site):
    return site.get('name') or site.get('rss_url') or 'site'


def parse_index(raw):
    try:
        return int(str(raw).strip())
    except Exception:
        return -1


def get_item_by_index(items, idx):
    if 0 <= idx < len(items):
        return items[idx]
    return None


def short_error(exc):
    return f'{exc.__class__.__name__}: {exc}'


def short_text(text, limit=3500):
    text = text or ''
    return text if len(text) <= limit else text[: limit - 3] + '...'


def html_escape(text):
    text = str(text or '')
    return (
        text.replace('&', '&amp;')
        .replace('<', '&lt;')
        .replace('>', '&gt;')
    )


def clip(text, limit=220):
    text = (text or '').replace('\r', ' ').replace('\n', ' ').strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3] + '...'


def feed_headers(cookie=''):
    headers = {
        'User-Agent': 'PTRSS/0.1',
        'Accept': 'application/rss+xml, application/xml, text/xml, application/atom+xml, text/html;q=0.9, */*;q=0.8',
    }
    if cookie:
        headers['Cookie'] = cookie
    return headers


def fetch_feed(site):
    site_name = site.get('name') or site.get('base_url') or 'site'
    rss_url = site.get('rss_url', '')
    log('feed request', site_name, rss_url)
    resp = requests.get(
        site['rss_url'],
        headers=feed_headers(site.get('cookie', '')),
        timeout=20,
        allow_redirects=True,
    )
    log('feed response', site_name, f'http={resp.status_code}', f"ct={resp.headers.get('content-type', '(unknown)')}", resp.url)
    return resp


def test_site_connectivity(site):
    resp = fetch_feed(site)
    content_type = resp.headers.get('content-type', '(unknown)')
    body_preview = clip(resp.text, 180)
    return {
        'ok': resp.ok,
        'message': f'站点测试 HTTP {resp.status_code} | Content-Type: {content_type} | 内容预览: {body_preview or "(empty)"}',
    }


def strip_ns(tag):
    return tag.split('}', 1)[-1] if '}' in tag else tag


def first_text(node, *names):
    wanted = set(names)
    for child in list(node):
        if strip_ns(child.tag) in wanted:
            text = ''.join(child.itertext()).strip()
            if text:
                return text
    return ''


def normalize_datetime_text(value):
    value = (value or '').strip()
    if not value:
        return ''
    try:
        return parsedate_to_datetime(value).strftime('%Y-%m-%d %H:%M:%S %z')
    except Exception:
        return value


def collect_items(root):
    items = []
    feed_type = ''
    feed_title = ''

    channel = None
    for child in list(root):
        if strip_ns(child.tag) == 'channel':
            channel = child
            break
    if strip_ns(root.tag) == 'rss' and channel is not None:
        feed_type = 'rss'
        feed_title = first_text(channel, 'title')
        item_nodes = [child for child in list(channel) if strip_ns(child.tag) == 'item']
    elif strip_ns(root.tag) == 'feed':
        feed_type = 'atom'
        feed_title = first_text(root, 'title')
        item_nodes = [child for child in list(root) if strip_ns(child.tag) == 'entry']
    else:
        item_nodes = [child for child in root.iter() if strip_ns(child.tag) in ('item', 'entry')]
        feed_type = strip_ns(root.tag).lower()

    for node in item_nodes[:100]:
        title = first_text(node, 'title') or '(无标题)'
        description = first_text(node, 'description', 'summary', 'content', 'subtitle')
        guid = first_text(node, 'guid', 'id')
        published_raw = first_text(node, 'pubDate', 'published', 'updated')
        published = normalize_datetime_text(published_raw)
        link = ''
        enclosure = ''
        for child in list(node):
            tag = strip_ns(child.tag)
            if tag == 'link':
                href = child.attrib.get('href')
                rel = child.attrib.get('rel', '')
                if href and (not link or rel in ('alternate', '')):
                    link = href.strip()
                elif not href:
                    text = ''.join(child.itertext()).strip()
                    if text and not link:
                        link = text
            elif tag == 'enclosure' and not enclosure:
                enclosure = child.attrib.get('url', '').strip()
        items.append({
            'title': title,
            'link': link,
            'guid': guid,
            'published': published,
            'published_raw': published_raw,
            'description': clip(description, 320),
            'enclosure': enclosure,
        })
    return feed_type, feed_title, items


def detect_feed_like_problem(raw_text, content_type=''):
    text = raw_text or ''
    stripped = text.lstrip('\ufeff\ufffe\u200b\x00\r\n\t ')
    low = stripped[:800].lower()
    ctype = (content_type or '').lower()
    if not stripped:
        return '空响应'
    if low.startswith('<!doctype html') or low.startswith('<html') or '<html' in low[:200]:
        if 'login' in low or 'signin' in low or '登录' in low:
            return '返回的是 HTML 登录页，不是 RSS XML'
        if 'cloudflare' in low or 'captcha' in low or 'challenge' in low or '安全验证' in low:
            return '返回的是 HTML 验证/挑战页，不是 RSS XML'
        return '返回的是 HTML 页面，不是 RSS XML'
    if 'xml' not in low[:120] and not stripped.startswith('<rss') and not stripped.startswith('<feed'):
        if 'text/html' in ctype:
            return 'Content-Type 是 HTML，不是 RSS XML'
        if stripped[0] != '<':
            return '响应开头不是 XML，可能是异常页或脏字符'
    return ''


def parse_feed_response(resp):
    raw_text = resp.text or ''
    content_type = resp.headers.get('content-type', '')
    raw_preview = raw_text[:4000] or '(empty)'
    if not resp.ok:
        return {
            'ok': False,
            'status_code': resp.status_code,
            'feed_type': '',
            'feed_title': '',
            'item_count': 0,
            'items': [],
            'raw_preview': raw_preview,
            'error': f'HTTP {resp.status_code}',
        }

    feed_problem = detect_feed_like_problem(raw_text, content_type)
    if feed_problem:
        return {
            'ok': False,
            'status_code': resp.status_code,
            'feed_type': '',
            'feed_title': '',
            'item_count': 0,
            'items': [],
            'raw_preview': raw_preview,
            'error': feed_problem,
        }

    cleaned_text = raw_text.lstrip('\ufeff\ufffe\u200b\x00\r\n\t ')
    try:
        root = ET.fromstring(cleaned_text.encode(resp.encoding or 'utf-8', errors='ignore'))
        feed_type, feed_title, items = collect_items(root)
        return {
            'ok': True,
            'status_code': resp.status_code,
            'feed_type': feed_type,
            'feed_title': feed_title,
            'item_count': len(items),
            'items': items,
            'raw_preview': raw_preview,
            'error': '' if items else 'XML 解析成功，但没有识别到 item/entry',
        }
    except Exception as exc:
        detail = detect_feed_like_problem(cleaned_text, content_type)
        if detail:
            error_text = detail
        else:
            error_text = f'XML 解析失败：{short_error(exc)}'
        return {
            'ok': False,
            'status_code': resp.status_code,
            'feed_type': '',
            'feed_title': '',
            'item_count': 0,
            'items': [],
            'raw_preview': raw_preview,
            'error': error_text,
        }


def preview_site_feed(site):
    preview = parse_feed_response(fetch_feed(site))
    log('feed parsed', site.get('name') or site.get('base_url') or 'site', f"ok={preview.get('ok')}", f"items={preview.get('item_count')}", preview.get('error') or '')
    return preview


def dedupe_key(item):
    guid = (item.get('guid') or '').strip()
    if guid:
        return f'guid:{guid}'
    link = (item.get('link') or '').strip()
    if link:
        return f'link:{link}'
    title = (item.get('title') or '').strip()
    published = (item.get('published') or '').strip()
    return f'title:{title}|pub:{published}'


def compact_item(item):
    if not isinstance(item, dict):
        return {}
    return {
        'title': short_text((item.get('title') or '').strip(), 240),
        'link': short_text((item.get('link') or '').strip(), 400),
        'guid': short_text((item.get('guid') or '').strip(), 160),
        'published': short_text((item.get('published') or '').strip(), 120),
        'enclosure': short_text((item.get('enclosure') or '').strip(), 400),
    }


def parse_title(raw_title):
    raw_title = (raw_title or '').strip()
    if not raw_title:
        return {'category': '', 'main_title': '', 'sub_title': '', 'size': '', 'uploader': ''}

    m = re.match(r'^\[(.*?)\](.*?)\[(.*?)\]\[(.*?)\]\[(.*?)\]\s*$', raw_title)
    if m:
        return {
            'category': m.group(1).strip(),
            'main_title': m.group(2).strip(),
            'sub_title': m.group(3).strip(),
            'size': m.group(4).strip(),
            'uploader': m.group(5).strip(),
        }

    rest = raw_title
    category = ''
    m = re.match(r'^\[([^\]]+)\]\s*(.*)$', rest)
    if m:
        category = m.group(1).strip()
        rest = m.group(2).strip()

    tail_groups = []
    while rest.endswith(']'):
        m = re.search(r'\[([^\[\]]*)\]\s*$', rest)
        if not m:
            break
        tail_groups.insert(0, m.group(1).strip())
        rest = rest[:m.start()].strip()

    main_title = rest.strip()
    sub_title = ''
    size = ''
    uploader = ''

    if tail_groups:
        maybe_size = tail_groups[-1]
        if re.search(r'\b(?:\d+(?:\.\d+)?)\s*(?:[KMGTP]i?B|B)\b', maybe_size, flags=re.I):
            size = maybe_size
            tail_groups = tail_groups[:-1]
        if tail_groups:
            uploader = tail_groups[-1]
            tail_groups = tail_groups[:-1]
        if tail_groups:
            sub_title = ' | '.join([g for g in tail_groups if g])

    return {
        'category': category.strip(),
        'main_title': main_title.strip(),
        'sub_title': sub_title.strip(),
        'size': size.strip(),
        'uploader': uploader.strip(),
    }


def format_relative_time(pub_text):
    if not pub_text:
        return ''
    try:
        dt = parsedate_to_datetime(pub_text)
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        delta = max(0, int((now - dt).total_seconds()))
        if delta < 60:
            return f'{delta}秒前'
        if delta < 3600:
            return f'{delta // 60}分钟前'
        if delta < 86400:
            return f'{delta // 3600}小时前'
        return f'{delta // 86400}天前'
    except Exception:
        return ''


def get_detail_id(link):
    try:
        return parse_qs(urlparse(link).query).get('id', [''])[0]
    except Exception:
        return ''


def fetch_detail_html(site, link):
    if not link:
        return ''
    headers = {'User-Agent': 'PTRSS/0.1'}
    if site.get('cookie'):
        headers['Cookie'] = site.get('cookie', '')
    try:
        resp = requests.get(link, headers=headers, timeout=30, allow_redirects=True)
        if 'login' in resp.url.lower():
            log('detail fetched', site.get('name') or site.get('base_url') or 'site', 'redirected_to_login', resp.url)
            return ''
        resp.raise_for_status()
        log('detail fetched', site.get('name') or site.get('base_url') or 'site', f'http={resp.status_code}', resp.url)
        return resp.text or ''
    except Exception as exc:
        log('detail fetch failed', site.get('name') or site.get('base_url') or 'site', short_error(exc))
        return ''


def extract_media_links(text):
    if not text:
        return {'imdb': '', 'douban': ''}
    imdb = ''
    douban = ''
    for m in re.findall(r'https?://[^\s"\'\)<>]+', text, flags=re.I):
        m = m.rstrip('.,;!?)]')
        low = m.lower()
        if not imdb and 'imdb.' in low:
            imdb = m
        if not douban and ('douban.' in low or 'movie.douban.com' in low):
            douban = m
    return {'imdb': imdb, 'douban': douban}


def extract_poster_candidates(html_text, base_url=''):
    if not html_text:
        return []
    patterns = [
        r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']twitter:image["\'][^>]+content=["\']([^"\']+)["\']',
        r'"image"\s*:\s*"(https?:\\/\\/[^"\\]+(?:jpg|jpeg|png|webp)[^"\\]*)"',
        r'<img[^>]+(?:data-src|data-original|src)=["\']([^"\']+)["\']',
        r'\[img\]([^\[]+?)\[/img\]',
    ]
    found = []
    seen = set()
    for pattern in patterns:
        for url in re.findall(pattern, html_text, flags=re.I):
            url = (url or '').strip().replace('\\/', '/')
            if not url:
                continue
            if url.startswith('//'):
                url = 'https:' + url
            elif url.startswith('/') or (not re.match(r'^https?://', url, flags=re.I) and base_url):
                url = urljoin(base_url.rstrip('/') + '/', url.lstrip('/'))
            low = url.lower()
            if not url.startswith('http'):
                continue
            if any(x in low for x in ['sprite', 'icon', 'avatar', 'blank.gif', 'smilies', 'cattrans', 'trans.gif', 'bonus_', 'nophoto.gif']):
                continue
            if any(x in low for x in ['ourbits_info', 'ourbits_morescreens']):
                continue
            if url not in seen:
                seen.add(url)
                found.append(url)

    def score(url):
        low = url.lower()
        val = 0
        if 'proxy-cover.hhanclub.net/douban/' in low:
            val += 140
        if 'doubanio.com/view/photo' in low:
            val += 120
        if 'poster' in low or '/imdb/posters' in low:
            val += 100
        if 'img.hdsky.me/images/' in low:
            val += 80
        if any(x in low for x in ['amazonaws.com', 'm.media-amazon.com']):
            val += 70
        if '/attachments/' in low or 'attachments/' in low:
            val += 40
        if any(x in low for x in [
            'bitbucket/',
            '/chdbits.png',
            '/donate.gif',
            '/trans.gif',
            '/logo',
            'logo.',
            '/banner',
            'banner.',
            '/donate',
        ]):
            val -= 200
        if any(x in low for x in ['favicon', 'icon', 'badge', 'avatar']):
            val -= 120
        if '_thumb' in low:
            val -= 10
        return val

    return sorted(found, key=score, reverse=True)


def fetch_page_poster(page_url):
    if not page_url:
        return []
    headers = {'User-Agent': 'PTRSS/0.1'}
    page_url = (page_url or '').strip()
    if re.match(r'^https?://douban\.com/subject/', page_url, flags=re.I):
        page_url = re.sub(r'^https?://douban\.com/subject/', 'https://movie.douban.com/subject/', page_url, flags=re.I)
    low = page_url.lower()
    if 'douban.' in low:
        headers['Referer'] = 'https://movie.douban.com/'
    elif 'imdb.' in low:
        headers['Referer'] = 'https://www.imdb.com/'
    try:
        resp = requests.get(page_url, headers=headers, timeout=30, allow_redirects=True)
        resp.raise_for_status()
        posters = extract_poster_candidates(resp.text or '', resp.url)
        if posters:
            return posters
        if 'imdb.' in low:
            m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', resp.text or '', flags=re.I)
            if m:
                return [m.group(1)]
        if 'douban.' in low:
            m = re.search(r'https?://img\d+\.doubanio\.com/view/photo/[^\s"\'<>]+', resp.text or '', flags=re.I)
            if m:
                return [m.group(0)]
        return []
    except Exception:
        return []


def fetch_poster_binary(poster_url):
    headers = {'User-Agent': 'PTRSS/0.1'}
    low = poster_url.lower()
    if 'doubanio.com' in low or 'douban.com' in low:
        headers['Referer'] = 'https://movie.douban.com/'
    elif 'imdb.com' in low or 'media-amazon.com' in low:
        headers['Referer'] = 'https://www.imdb.com/'
    resp = requests.get(poster_url, headers=headers, timeout=60)
    resp.raise_for_status()
    return resp.content, resp.headers.get('content-type', 'image/jpeg')


SITE_DISPLAY_NAMES = {
    'HDSky': '天空',
    'SpringSunday': '春天',
    'KeepFrds': '朋友',
    'Audiences': '观众',
    'OurBits': '我堡',
    'HHanClub': '憨憨',
    'PTchdBits': '彩虹岛',
    'Piggo': '猪猪',
}


def clean_html_text(text):
    text = re.sub(r'<br\s*/?>', '\n', text or '', flags=re.I)
    text = re.sub(r'</?(?:p|div|li|dt|dd|tr|td|th|h\d)[^>]*>', '\n', text, flags=re.I)
    text = re.sub(r'<script[\s\S]*?</script>', ' ', text, flags=re.I)
    text = re.sub(r'<style[\s\S]*?</style>', ' ', text, flags=re.I)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&nbsp;', ' ')
    text = re.sub(r'\r', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n', text)
    text = re.sub(r'[ \t]+', ' ', text)
    return text.strip()


def pick_first(*values):
    for value in values:
        value = (value or '').strip()
        if value:
            return value
    return ''


def match_text(pattern, text, flags=re.I | re.S):
    m = re.search(pattern, text or '', flags)
    if not m:
        return ''
    if m.lastindex:
        return clean_html_text(m.group(1))
    return clean_html_text(m.group(0))


def normalize_site_name(site_name):
    return SITE_DISPLAY_NAMES.get(site_name, site_name)


def build_short_detail_url(detail_id):
    return f"{PUBLIC_BASE_URL.rstrip('/')}/go/{detail_id}"


def extract_light_fields(site_name, item, detail_html=''):
    raw_title = clean_html_text(item.get('title') or '')
    html = detail_html or ''

    detail_title = clean_html_text(match_text(r'<h1[^>]*id=["\']top["\'][^>]*>(.*?)</h1>', html))
    detail_title = re.sub(r'\s*(?:\[\s*免费\s*\].*|\(限时.*?\)|剩余时间：.*)$', '', detail_title).strip()
    detail_subtitle = clean_html_text(match_text(r'副标题</td><td[^>]*>(.*?)</td>', html))
    title_tag = clean_html_text(match_text(r'<title>(.*?)</title>', html))
    basic_info = match_text(r'基本信息</td><td[^>]*>(.*?)</td>', html)

    parsed_title = parse_title(raw_title)
    title = detail_title or raw_title or '(无标题)'
    subtitle = detail_subtitle
    is_free = 'free' in html.lower() or '免费' in raw_title or '促销剩余时间' in (item.get('description') or '') or '免费' in detail_html or '限时' in detail_html
    size = pick_first(
        match_text(r'大小[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
        match_text(r'\[(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|Ti?B|KB|MB|GB|TB))\]', item.get('title') or ''),
        match_text(r'(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|Ti?B|KB|MB|GB|TB))', item.get('description') or ''),
        item.get('size'),
        parsed_title.get('size', ''),
    )

    if not title:
        title = title_tag
    if not detail_title and title_tag:
        m = re.search(r'种子详情\s*["“]?([^"”]+)["”]?', title_tag)
        if m:
            title = clean_html_text(m.group(1))

    if site_name in ('KeepFrds',):
        if detail_title and detail_subtitle:
            title = detail_title
            subtitle = detail_subtitle
    elif site_name in ('OurBits', 'SpringSunday', 'PTchdBits', 'HDSky'):
        title = detail_title or title
        subtitle = detail_subtitle or subtitle
    elif site_name in ('HHanClub', 'Piggo'):
        if not title or title == raw_title:
            m = re.search(r'种子详情\s*["“]?([^"”]+)["”]?', title_tag)
            if m:
                title = clean_html_text(m.group(1))
        if not subtitle:
            subtitle = clean_html_text(parsed_title.get('sub_title') or '')
    else:
        if not subtitle:
            subtitle = clean_html_text(parsed_title.get('sub_title') or '')

    if not subtitle:
        desc = clean_html_text(item.get('description') or '')
        desc = re.sub(r'\s*see:\s*https?://\S+', '', desc, flags=re.I).strip()
        if desc and len(desc) <= 120 and '<img' not in (item.get('description') or '').lower():
            subtitle = desc

    title = short_text(clean_html_text(title), 220)
    subtitle = short_text(clean_html_text(subtitle), 260)
    size = short_text(clean_html_text(str(size or '')), 60)
    if subtitle == title:
        subtitle = ''
    return {
        'site': normalize_site_name(site_name),
        'title': title,
        'subtitle': subtitle,
        'size': size,
        'is_free': is_free,
        'published_relative': format_relative_time(item.get('published_raw') or item.get('published') or ''),
    }


def guess_title_year(text):
    text = clean_html_text(text)
    if not text:
        return '', ''
    year = ''
    m = re.search(r'(19\d{2}|20\d{2}|21\d{2})', text)
    if m:
        year = m.group(1)
    title = text
    title = re.sub(r'\bS\d{1,2}E\d{1,3}(?:-E?\d{1,3})?\b', ' ', title, flags=re.I)
    title = re.sub(r'\b(?:19\d{2}|20\d{2}|21\d{2})\b', ' ', title)
    title = re.sub(r'\b(?:2160p|1080p|1080i|720p|WEB[- ]DL|WEBRip|BluRay|Remux|H\.265|H265|H\.264|H264|x265|x264|DDP?\d(?:\.\d)?|Atmos|AAC\d(?:\.\d)?|DTS(?:-HD)?|TrueHD)\b', ' ', title, flags=re.I)
    title = re.sub(r'[-_.]+', ' ', title)
    title = re.sub(r'\s{2,}', ' ', title).strip(' -_|')
    return title.strip(), year


def extract_episode_text(*texts):
    joined = ' | '.join([clean_html_text(t) for t in texts if t])
    if not joined:
        return ''
    m = re.search(r'S\d{1,2}E(\d{1,3})(?:-E?(\d{1,3}))?', joined, flags=re.I)
    if m:
        return f'第{m.group(1)}-{m.group(2)}集' if m.group(2) else f'第{m.group(1)}集'
    m = re.search(r'第\s*(\d{1,3})(?:\s*[-~至到]\s*(\d{1,3}))?\s*集', joined, flags=re.I)
    if m:
        return f'第{m.group(1)}-{m.group(2)}集' if m.group(2) else f'第{m.group(1)}集'
    return ''


def is_episode_item(item):
    if not isinstance(item, dict):
        return False
    texts = [
        item.get('title') or '',
        item.get('description') or '',
        item.get('link') or '',
        item.get('guid') or '',
    ]
    joined = ' | '.join(clean_html_text(x) for x in texts if x)
    if not joined:
        return False
    if re.search(r'剧集\s*[(/（]\s*分集', joined, flags=re.I):
        return True
    if re.search(r'海外剧集\s*[(/（]\s*分集', joined, flags=re.I):
        return True
    if re.search(r'\bS\d{1,2}E\d{1,3}(?:-E?\d{1,3})?\b', joined, flags=re.I):
        return True
    if re.search(r'第\s*\d{1,3}(?:\s*[-~至到]\s*\d{1,3})?\s*集', joined, flags=re.I):
        return True
    return False


def item_age_seconds(item):
    if not isinstance(item, dict):
        return None
    pub_text = (item.get('published_raw') or item.get('published') or '').strip()
    if not pub_text:
        return None
    try:
        dt = parsedate_to_datetime(pub_text)
        now = datetime.now(dt.tzinfo) if dt.tzinfo else datetime.now()
        return max(0, int((now - dt).total_seconds()))
    except Exception:
        return None


def is_recent_item(item, max_age_seconds=24 * 3600):
    age = item_age_seconds(item)
    if age is None:
        return True
    return age <= max_age_seconds


def normalize_tag_text(tag):
    tag = clean_html_text(tag)
    low = tag.lower()
    mapping = {
        'diy': 'DIY',
        'hdr': 'HDR',
        'hdr10': 'HDR10',
        'dv': '杜比视界',
    }
    return mapping.get(low, tag)


def dedupe_tags(tags):
    result = []
    seen = set()
    for tag in tags:
        norm = normalize_tag_text(tag)
        key = norm.lower()
        if not norm or key in seen:
            continue
        seen.add(key)
        result.append(norm)
    return result


def extract_ourbits_description(html):
    raw = match_text(r"<div id='kdescr'>(.*?)</div>", html)
    if not raw:
        return ''
    raw = clean_html_text(raw)
    m = re.search(r'◎简\s*介\s*(.*?)(?:DISC INFO:|◎|$)', raw, flags=re.S)
    if m:
        return clean_html_text(m.group(1))
    lines = []
    for line in raw.split('\n'):
        line = clean_html_text(line)
        if not line:
            continue
        if any(x in line for x in ['禁止转载', 'exclusive', '联系QQ', '字幕校对', 'HDR模式下播放', '未经许可禁止', 'DISC INFO:']):
            continue
        if line.startswith('◎') or line.startswith('Disc '):
            continue
        lines.append(line)
    return short_text(' '.join(lines[:3]), 320)


def extract_ourbits_title_year(item, html, subtitle):
    raw_title = clean_html_text(item.get('title') or '')
    eng = match_text(r'◎片\s*名\s*(.*?)(?:◎|$)', html)
    zh = pick_first(
        match_text(r'◎译\s*名\s*(.*?)(?:◎|$)', html).split('/')[0].strip(),
        subtitle,
    )
    year = pick_first(match_text(r'◎年\s*代\s*(\d{4})', html), guess_title_year(raw_title)[1])
    title = pick_first(zh, eng)
    if not title:
        title = subtitle or raw_title
    title = re.sub(r'\[[^\]]+\]', ' ', title)
    title = re.sub(r'\s{2,}', ' ', title).strip(' -_|')
    return short_text(title, 160), year


def extract_detail_fields(site_name, item, detail_html):
    html = detail_html or ''
    desc = clean_html_text(item.get('description') or '')
    subtitle = pick_first(
        match_text(r'副标题</td><td[^>]*>(.*?)</td>', html),
        match_text(r'<dt>\s*译名\s*</dt>\s*<dd[^>]*>(.*?)</dd>', html),
    )

    if site_name == 'OurBits':
        title, year = extract_ourbits_title_year(item, html, subtitle)
        basic_info = match_text(r'基本信息</td><td[^>]*>(.*?)</td>', html)
        basic_text = clean_html_text(basic_info)
        quality_parts = []
        medium = pick_first(
            match_text(r'媒介[:：]?\s*([^\n]+?)(?:\s+编码[:：]?|\s+音频编码[:：]?|\s+分辨率[:：]?|$)', basic_text),
            match_text(r'媒介[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
        )
        standard = pick_first(
            match_text(r'分辨率[:：]?\s*([^\n]+?)(?:\s+地区[:：]?|\s+制作组[:：]?|$)', basic_text),
            match_text(r'分辨率[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
        )
        for part in [medium, standard]:
            if part and part not in quality_parts:
                quality_parts.append(part.replace('UHD Blu-ray', 'BluRay').replace('UHD BluRay', 'BluRay'))
        tags = []
        tags_html_raw = pick_first(
            match_text(r'(?:Tags|标签)</td><td[^>]*>(.*?)</td>', html),
            '',
        )
        for x in re.findall(r'<span[^>]*>(.*?)</span>', html, flags=re.I | re.S):
            val = clean_html_text(x)
            if val in ['官方', 'DIY', '中字', '禁转', '杜比视界', 'HDR10', 'HDR']:
                tags.append(val)
        tags = dedupe_tags(tags)
        return {
            'title': short_text(title, 160),
            'year': year,
            'site': normalize_site_name(site_name),
            'quality': ' '.join(quality_parts).strip(),
            'size': pick_first(
                match_text(r'大小[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
                match_text(r'\[(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|Ti?B|KB|MB|GB|TB))\]', item.get('title') or ''),
                match_text(r'(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|Ti?B|KB|MB|GB|TB))', item.get('description') or ''),
                parse_title(item.get('title') or '').get('size', ''),
            ),
            'torrent_name': short_text(clean_html_text(item.get('title') or ''), 220),
            'published': item.get('published') or normalize_datetime_text(item.get('published_raw') or ''),
            'seeders': pick_first(
                match_text(r'title=["\']当前做种["\'][^>]*src=["\'][^"\']+["\'][^>]*\/>\s*(\d+)', html),
                match_text(r'当前做种[^\d]{0,20}(\d+)', html),
            ),
            'promo': pick_first(
                match_text(r'class=["\']free["\'][^>]*>\s*([^<]+)\s*<', html),
                match_text(r'剩余时间：', html) and '免费' or '',
            ),
            'tags': ' '.join(tags[:8]),
            'description': short_text(extract_ourbits_description(html), 320),
        }

    db_title = pick_first(
        match_text(r'<dt>\s*片名\s*</dt>\s*<dd[^>]*>(.*?)</dd>', html),
        match_text(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', html),
    )
    title_guess, year_guess = guess_title_year(' | '.join([db_title, subtitle, item.get('title') or '', desc]))
    year = pick_first(
        match_text(r'<dt>\s*年代\s*</dt>\s*<dd[^>]*>(.*?)</dd>', html),
        year_guess,
    )
    title = pick_first(title_guess, db_title, subtitle, item.get('title') or '(无标题)')
    episode_text = extract_episode_text(item.get('title'), subtitle, desc)
    if episode_text and episode_text not in title:
        title = f'{title} {episode_text}'.strip()
    quality_parts = []
    basic_info = match_text(r'基本信息</td><td[^>]*>(.*?)</td>', html)
    for part in [
        match_text(r'媒介[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
        match_text(r'分辨率[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
    ]:
        if part and part not in quality_parts:
            quality_parts.append(part)
    if not quality_parts:
        raw = item.get('title') or ''
        for token in ['WEB-DL', 'WEBRip', 'BluRay', 'Remux', '2160p', '1080p', '1080i', '720p']:
            if re.search(re.escape(token), raw, flags=re.I) and token not in quality_parts:
                quality_parts.append(token)
    quality = ' '.join(quality_parts).strip()
    size = pick_first(
        match_text(r'大小[:：]?\s*</?b?>?\s*([^<\s][^<&]{0,40})', basic_info),
        match_text(r'\[(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|Ti?B|KB|MB|GB|TB))\]', item.get('title') or ''),
        match_text(r'(\d+(?:\.\d+)?\s*(?:Ki?B|Mi?B|Gi?B|Ti?B|KB|MB|GB|TB))', item.get('description') or ''),
        item.get('size'),
        parse_title(item.get('title') or '').get('size', ''),
    )
    seeders = pick_first(
        match_text(r'title=["\']当前做种["\'][^>]*src=["\'][^"\']+["\'][^>]*\/>\s*(\d+)', html),
        match_text(r'当前做种[^\d]{0,20}(\d+)', html),
    )
    promo = pick_first(
        match_text(r'class=["\']free["\'][^>]*>\s*([^<]+)\s*<', html),
        match_text(r'促销[^\u4e00-\u9fa5A-Za-z0-9]{0,10}([\u4e00-\u9fa5A-Za-z0-9%×xX\-]+)', html),
    )
    tags = []
    for pat in [r'>\s*(官组|禁转|国语|中字|DIY|HDR|杜比视界|国配|粤语|英字|内封[^<\s]*|简繁英多国软字幕|去头尾广告[^<]*)\s*<', r'\b(官组|禁转|国语|中字|DIY|HDR)\b']:
        for x in re.findall(pat, html, flags=re.I):
            val = clean_html_text(x)
            if val and val not in tags:
                tags.append(val)
    description = pick_first(
        match_text(r'<dt>\s*简介\s*</dt>\s*<dd[^>]*>(.*?)</dd>', html),
        desc,
    )
    published = item.get('published') or normalize_datetime_text(item.get('published_raw') or '')
    return {
        'title': short_text(title, 160),
        'year': year,
        'site': normalize_site_name(site_name),
        'quality': quality,
        'size': size,
        'torrent_name': short_text(item.get('title') or '', 220),
        'published': published,
        'seeders': seeders,
        'promo': promo,
        'tags': ' '.join(dedupe_tags(tags)[:8]),
        'description': short_text(description, 320),
    }


def build_pretty_telegram_message(site_name, item, detail_html=''):
    info = extract_light_fields(site_name, item, detail_html)
    lines = [
        f"站点：{info.get('site') or normalize_site_name(site_name)}",
        f"主标题：{info.get('title') or '(无标题)'}",
    ]
    if info.get('subtitle'):
        lines.append(f"副标题：{info['subtitle']}")
    else:
        lines.append("副标题：")
    if info.get('size'):
        lines.append(f"大小：{info['size']}")
    lines.append(f"是否free：{'免费' if info.get('is_free') else '非免费'}")
    if info.get('published_relative'):
        lines.append(f"发布时间：{info['published_relative']}")
    return short_text('\n'.join(lines), 1024)


def should_auto_dispatch_all_downloaders(site_name, item, detail_html=''):
    site_key = (site_name or '').strip().lower()
    if site_key not in ('frds', 'keepfrds'):
        return False
    info = extract_light_fields(site_name, item, detail_html)
    return bool(info.get('is_free'))


def remember_entry(state, site_name, item):
    detail_id = get_detail_id(item.get('link') or '') or dedupe_key(item).replace(':', '_').replace('|', '_')[:120]
    state.setdefault('entry_map', {})[detail_id] = {
        'site_name': site_name,
        'title': item.get('title', ''),
        'link': item.get('link', ''),
        'download_url': item.get('enclosure', ''),
        'published': item.get('published', ''),
        'saved_at': now_text(),
    }
    return detail_id


def tg_api(method, **data):
    cfg = load_config()
    token = cfg['telegram'].get('bot_token', '').strip()
    if not token:
        raise RuntimeError('Telegram Bot Token 为空')
    url = f'https://api.telegram.org/bot{token}/{method}'
    resp = requests.post(url, data=data, timeout=30)
    resp.raise_for_status()
    payload = resp.json()
    if not payload.get('ok'):
        raise RuntimeError(str(payload)[:300])
    return payload


def register_tg_commands(cfg=None):
    cfg = cfg or load_config()
    if not cfg.get('telegram', {}).get('enabled'):
        return {'ok': False, 'message': 'Telegram 未启用'}
    commands = [
        {'command': 'rss', 'description': '查看 PTRSS 状态'},
        {'command': 'rsspoll', 'description': '手动轮询一次'},
        {'command': 'rsson', 'description': '开启 PTRSS 推送'},
        {'command': 'rssoff', 'description': '关闭 PTRSS 推送'},
        {'command': 'rsshelp', 'description': '查看可用命令'},
    ]
    tg_api('setMyCommands', commands=json.dumps(commands, ensure_ascii=False))
    return {'ok': True, 'message': 'Telegram 菜单命令已注册'}


def send_telegram_message(cfg, text, reply_markup=None, poster_urls=None, ignore_push_switch=False):
    token = cfg['telegram'].get('bot_token', '').strip()
    chat_id = cfg['telegram'].get('chat_id', '').strip()
    if not cfg['telegram'].get('enabled'):
        return {'ok': False, 'message': 'Telegram 未启用'}
    if not ignore_push_switch and not cfg.get('base', {}).get('push_enabled', True):
        return {'ok': False, 'message': 'TG 推送开关当前已关闭'}
    if not token or not chat_id:
        return {'ok': False, 'message': 'Telegram Bot Token 或 Chat ID 为空'}
    if poster_urls:
        for poster_url in poster_urls[:3]:
            try:
                content, content_type = fetch_poster_binary(poster_url)
                ext = '.jpg'
                if 'png' in content_type.lower():
                    ext = '.png'
                files = {'photo': ('poster' + ext, content, content_type)}
                data = {'chat_id': chat_id, 'caption': text}
                if reply_markup:
                    data['reply_markup'] = reply_markup
                resp = requests.post(f'https://api.telegram.org/bot{token}/sendPhoto', data=data, files=files, timeout=60)
                payload = resp.json()
                if resp.ok and isinstance(payload, dict) and payload.get('ok') is True:
                    log('telegram sent', 'photo', f"message_id={((payload.get('result') or {}).get('message_id') or '')}")
                    return {'ok': True, 'message': 'Telegram 已发送(photo)'}
            except Exception:
                pass
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    data = {'chat_id': chat_id, 'text': text, 'disable_web_page_preview': 'true'}
    if reply_markup:
        data['reply_markup'] = reply_markup
    resp = requests.post(url, data=data, timeout=20)
    try:
        payload = resp.json()
    except Exception:
        payload = None
    ok = resp.ok and isinstance(payload, dict) and payload.get('ok') is True
    if ok:
        log('telegram sent', 'text', f"message_id={((payload.get('result') or {}).get('message_id') or '')}")
        return {'ok': True, 'message': 'Telegram 已发送'}
    return {'ok': False, 'message': f'Telegram 发送失败：HTTP {resp.status_code} | {str(payload)[:220] if payload is not None else (resp.text or "(empty)")[:220]}'}


def build_download_keyboard(detail_id, detail_url=''):
    row = []
    if detail_url:
        row.append({'text': '查看详情', 'url': detail_url})
    row.append({'text': '下载', 'callback_data': f'pick|{detail_id}'})
    return json.dumps({'inline_keyboard': [row]}, ensure_ascii=False)


def build_poll_summary_message(trigger, per_site, total_fetched, total_new, total_skipped_episode_notify, total_errors):
    trigger_name = {
        'auto': '轮询完成',
        'manual-web': '手动轮询完成',
        'manual-tg': '手动轮询完成',
        'manual': '手动轮询完成',
    }.get(trigger, '轮询完成')

    if total_errors <= 0:
        return trigger_name

    details = []
    for item in per_site:
        name = item.get('site_name') or '未命名站点'
        status = item.get('status')
        if status in ('error', 'failed'):
            details.append(f'{name}：{item.get("error") or "异常"}')

    if details:
        return short_text(f'{trigger_name}，但有 {total_errors} 个站点异常\n' + '\n'.join(details[:6]), 900)
    return f'{trigger_name}，但有 {total_errors} 个站点异常'


def build_downloader_keyboard(detail_id, downloaders):
    rows = []
    row = []
    for idx, downloader in enumerate(downloaders):
        if not downloader.get('enabled'):
            continue
        name = (downloader.get('name') or f'QB{idx + 1}').strip()[:20]
        row.append({'text': name, 'callback_data': f'dl|{detail_id}|{idx}'})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return json.dumps({'inline_keyboard': rows}, ensure_ascii=False)


def build_control_keyboard(cfg):
    push_enabled = bool(cfg.get('base', {}).get('push_enabled', True))
    push_text = '推送开' if push_enabled else '推送关'
    return json.dumps({
        'inline_keyboard': [
            [
                {'text': f'{push_text}', 'callback_data': 'ptrss_toggle_push'},
                {'text': '轮询', 'callback_data': 'ptrss_run_poll'},
                {'text': '状态', 'callback_data': 'ptrss_status'},
            ],
        ]
    }, ensure_ascii=False)


def build_status_text(cfg, state):
    meta = state.get('meta', {}) if isinstance(state, dict) else {}
    enabled_sites = [site for site in cfg.get('sites', []) if site.get('enabled') and site.get('rss_url')]
    enabled_downloaders = [dl for dl in cfg.get('downloaders', []) if dl.get('enabled')]
    push_text = '开' if cfg.get('base', {}).get('push_enabled', True) else '关'
    poll_seconds = cfg.get('base', {}).get('poll_seconds', 300)
    last_poll_at = meta.get('last_poll_at') or '暂无'
    last_poll_summary = meta.get('last_poll_summary') or '暂无'
    lines = [
        'PTRSS',
        f'推送 {push_text}｜轮询 {poll_seconds}s',
        f'站点 {len(enabled_sites)}/{len(cfg.get("sites", []))}｜下载器 {len(enabled_downloaders)}/{len(cfg.get("downloaders", []))}',
        f'最近：{last_poll_at}',
        f'摘要：{last_poll_summary}',
    ]
    return short_text('\n'.join(lines), 1200)


def send_control_panel(chat_id, notice='', with_keyboard=False):
    cfg = load_config()
    state = load_state()
    text = build_status_text(cfg, state)
    if notice:
        text = f'{notice}\n\n{text}'
    tg_api('sendMessage', chat_id=chat_id, text=text, reply_markup=build_control_keyboard(cfg) if with_keyboard else None, disable_web_page_preview='true')


def download_torrent(download_url, cookie=''):
    headers = {'User-Agent': 'PTRSS/0.1'}
    if cookie:
        headers['Cookie'] = cookie
    resp = requests.get(download_url, headers=headers, timeout=60, allow_redirects=True)
    resp.raise_for_status()
    content = resp.content or b''
    if len(content) < 50 or b'announce' not in content:
        sample = content[:200].decode('utf-8', errors='ignore')
        raise RuntimeError(f'下载到的不是有效 torrent 文件: {sample}')
    return content


def qb_add_torrent(downloader, torrent_bytes, rename='rss.torrent', upload_limit_kib=None):
    base = (downloader.get('url') or '').strip().rstrip('/')
    username = str(downloader.get('username') or '').strip()
    password = str(downloader.get('password') or '').strip()
    if not base or not username:
        raise RuntimeError('下载器配置不完整：缺少 url/username')
    session = requests.Session()
    session.headers.update({'User-Agent': 'PTRSS/0.1'})
    login = session.post(f'{base}/api/v2/auth/login', data={'username': username, 'password': password}, timeout=30)
    login.raise_for_status()
    if login.text.strip() != 'Ok.':
        raise RuntimeError(f'qB 登录失败: {login.text[:200]}')
    data = {}
    for src, dst in [('category', 'category'), ('tags', 'tags'), ('savepath', 'savepath')]:
        val = str(downloader.get(src) or '').strip()
        if val:
            data[dst] = val
    files = {'torrents': (rename, torrent_bytes, 'application/x-bittorrent')}
    r = session.post(f'{base}/api/v2/torrents/add', data=data, files=files, timeout=60)
    r.raise_for_status()
    text = (r.text or '').strip()
    if text not in ('', 'Ok.'):
        raise RuntimeError(f'qB 添加失败: {text[:200]}')

    if upload_limit_kib is None:
        return

    hashes_resp = session.post(f'{base}/api/v2/torrents/info', data={'filter': 'all', 'sort': 'added_on', 'reverse': 'true', 'limit': 8}, timeout=30)
    hashes_resp.raise_for_status()
    infos = hashes_resp.json() if hashes_resp.text.strip() else []
    target_hash = ''
    expected_name = rename.rsplit('.', 1)[0]
    for info in infos:
        name = str((info or {}).get('name') or '')
        torrent_hash = str((info or {}).get('hash') or '')
        if not torrent_hash:
            continue
        if expected_name and expected_name in name:
            target_hash = torrent_hash
            break
    if not target_hash and infos:
        target_hash = str((infos[0] or {}).get('hash') or '')
    if not target_hash:
        raise RuntimeError('qB 已添加种子，但未找到对应任务用于设置上传限速')

    limit_bytes = max(0, int(upload_limit_kib)) * 1024
    limit_resp = session.post(
        f'{base}/api/v2/torrents/setUploadLimit',
        data={'hashes': target_hash, 'limit': str(limit_bytes)},
        timeout=30,
    )
    limit_resp.raise_for_status()
    limit_text = (limit_resp.text or '').strip()
    if limit_text not in ('', 'Ok.'):
        raise RuntimeError(f'qB 设置上传限速失败: {limit_text[:200]}')


def answer_callback(callback_id, text='', show_alert=False):
    try:
        tg_api('answerCallbackQuery', callback_query_id=callback_id, text=text[:180], show_alert='true' if show_alert else 'false')
    except Exception as exc:
        log('answer callback failed', short_error(exc))


def get_tg_offset():
    if not TG_OFFSET_FILE.exists():
        return 0
    try:
        return int(TG_OFFSET_FILE.read_text(encoding='utf-8').strip() or '0')
    except Exception:
        return 0


def set_tg_offset(offset):
    TG_OFFSET_FILE.write_text(str(offset), encoding='utf-8')


def set_push_enabled(enabled):
    cfg = load_config()
    cfg.setdefault('base', {})['push_enabled'] = bool(enabled)
    save_config(cfg)
    log('set push switch', 'enabled=', cfg['base']['push_enabled'])
    return cfg


def normalize_tg_command(text):
    text = (text or '').strip()
    if not text.startswith('/'):
        return ''
    cmd = text.split()[0].split('@')[0].strip().lower()
    return cmd


def handle_message(msg):
    chat_id = (msg.get('chat') or {}).get('id')
    text = (msg.get('text') or '').strip()
    if not chat_id or not text:
        return
    cfg = load_config()
    if str(chat_id) != str(cfg['telegram'].get('chat_id') or ''):
        return

    state = load_state()
    pending = state.get('pending_upload_limits', {}).get(str(chat_id))
    if pending:
        raw = text.strip()
        if raw.lower() in ('cancel', '取消'):
            state.get('pending_upload_limits', {}).pop(str(chat_id), None)
            save_state(state)
            tg_api('sendMessage', chat_id=chat_id, text='已取消这次下载提交。')
            return
        if not re.fullmatch(r'\d+', raw):
            tg_api('sendMessage', chat_id=chat_id, text='请输入上传限速数字，单位 kB/s。比如 500；输入 0 表示不限速；输入 取消 可放弃。')
            return
        entry = state.get('entry_map', {}).get(pending.get('detail_id', ''))
        idx = int(pending.get('downloader_idx', -1))
        downloaders = cfg.get('downloaders', [])
        if not entry or idx < 0 or idx >= len(downloaders) or not downloaders[idx].get('enabled'):
            state.get('pending_upload_limits', {}).pop(str(chat_id), None)
            save_state(state)
            tg_api('sendMessage', chat_id=chat_id, text='待提交记录已失效，请重新点一次下载。')
            return
        downloader = downloaders[idx]
        upload_limit_kib = int(raw)
        try:
            cookie = ''
            for site in cfg.get('sites', []):
                if site.get('name') == entry.get('site_name'):
                    cookie = site.get('cookie', '')
                    break
            torrent_bytes = download_torrent(entry.get('download_url', ''), cookie=cookie)
            qb_add_torrent(downloader, torrent_bytes, rename=f'{pending.get("detail_id")}.torrent', upload_limit_kib=upload_limit_kib)
            state.get('pending_upload_limits', {}).pop(str(chat_id), None)
            save_state(state)
            limit_text = '不限速' if upload_limit_kib == 0 else f'{upload_limit_kib} kB/s'
            tg_api('sendMessage', chat_id=chat_id, text=f'已提交到 {downloader.get("name") or f"QB{idx + 1}"}\n上传限速：{limit_text}')
        except Exception as exc:
            state.get('pending_upload_limits', {}).pop(str(chat_id), None)
            save_state(state)
            err = f'提交失败：{short_error(exc)}'
            log('download dispatch failed', err)
            tg_api('sendMessage', chat_id=chat_id, text=err[:900])
        return

    cmd = normalize_tg_command(text)
    if not cmd:
        return
    if cmd in ('/ptrss', '/rssmenu', '/rss', '/start', '/ptrss_status', '/rssstatus'):
        send_control_panel(chat_id, with_keyboard=False)
        return
    if cmd in ('/ptrss_on', '/rsson', '/rssstart'):
        set_push_enabled(True)
        send_control_panel(chat_id, '已打开 PTRSS TG 推送。', with_keyboard=False)
        return
    if cmd in ('/ptrss_off', '/rssoff', '/rssstop'):
        set_push_enabled(False)
        send_control_panel(chat_id, '已关闭 PTRSS TG 推送。', with_keyboard=False)
        return
    if cmd in ('/ptrss_poll', '/rsspoll'):
        tg_api('sendMessage', chat_id=chat_id, text='开始轮询，跑完告诉你结果。')
        summary = run_poll_cycle(trigger='manual-tg')
        send_control_panel(chat_id, summary, with_keyboard=False)
        return
    if cmd in ('/ptrss_help', '/rsshelp'):
        tg_api('sendMessage', chat_id=chat_id, text='可用命令：/rss /rsspoll /rsson /rssoff /rsshelp')
        return
    tg_api('sendMessage', chat_id=chat_id, text='没认出这个命令。可用命令：/rss /rsspoll /rsson /rssoff /rsshelp')
    return


def handle_callback_query(cb):
    callback_id = cb.get('id')
    data = (cb.get('data') or '').strip()
    msg = cb.get('message') or {}
    chat_id = (msg.get('chat') or {}).get('id')
    if not data or not chat_id:
        if callback_id:
            answer_callback(callback_id, '无效操作', show_alert=False)
        return
    cfg = load_config()
    if str(chat_id) != str(cfg['telegram'].get('chat_id') or ''):
        if callback_id:
            answer_callback(callback_id, '无权限', show_alert=True)
        return
    state = load_state()
    parts = data.split('|')
    action = parts[0]
    if action == 'ptrss_toggle_push':
        current = bool(cfg.get('base', {}).get('push_enabled', True))
        cfg = set_push_enabled(not current)
        status_text = '开启' if cfg.get('base', {}).get('push_enabled', True) else '关闭'
        answer_callback(callback_id, f'推送已{status_text}', show_alert=False)
        send_control_panel(chat_id, f'PTRSS TG 推送已{status_text}。', with_keyboard=False)
        return
    if action == 'ptrss_status':
        answer_callback(callback_id, '已刷新状态', show_alert=False)
        send_control_panel(chat_id, with_keyboard=False)
        return
    if action == 'ptrss_run_poll':
        answer_callback(callback_id, '开始轮询', show_alert=False)
        tg_api('sendMessage', chat_id=chat_id, text='开始轮询，跑完告诉你结果。')
        summary = run_poll_cycle(trigger='manual-tg')
        send_control_panel(chat_id, summary, with_keyboard=False)
        return
    if action == 'pick' and len(parts) >= 2:
        detail_id = parts[1]
        entry = state.get('entry_map', {}).get(detail_id)
        if not entry:
            answer_callback(callback_id, '这条记录过期了', show_alert=True)
            return
        downloaders = [d for d in cfg.get('downloaders', []) if d.get('enabled')]
        if not downloaders:
            answer_callback(callback_id, '还没配置可用下载器', show_alert=True)
            return
        tg_api('sendMessage', chat_id=chat_id, text=f'选择下载器\n{entry.get("title") or "未命名资源"}', reply_markup=build_downloader_keyboard(detail_id, cfg.get('downloaders', [])))
        answer_callback(callback_id, '请选择下载器', show_alert=False)
        return
    if action == 'dl' and len(parts) >= 3:
        detail_id = parts[1]
        entry = state.get('entry_map', {}).get(detail_id)
        if not entry:
            answer_callback(callback_id, '这条记录过期了', show_alert=True)
            return
        try:
            idx = int(parts[2])
        except Exception:
            answer_callback(callback_id, '下载器参数错误', show_alert=True)
            return
        downloaders = cfg.get('downloaders', [])
        if idx < 0 or idx >= len(downloaders) or not downloaders[idx].get('enabled'):
            answer_callback(callback_id, '下载器不存在', show_alert=True)
            return
        state.setdefault('pending_upload_limits', {})[str(chat_id)] = {
            'detail_id': detail_id,
            'downloader_idx': idx,
            'saved_at': now_text(),
        }
        save_state(state)
        downloader = downloaders[idx]
        tg_api('sendMessage', chat_id=chat_id, text=f'已选择下载器：{downloader.get("name") or f"QB{idx + 1}"}\n请输入上传限速，单位 kB/s\n例如：500\n输入 0 表示不限速\n输入 取消 可放弃')
        answer_callback(callback_id, '请继续输入上传限速', show_alert=False)
        return
    answer_callback(callback_id, '未知操作', show_alert=False)


def poll_telegram_once():
    cfg = load_config()
    if not cfg.get('telegram', {}).get('enabled'):
        return
    offset = get_tg_offset()
    try:
        data = tg_api('getUpdates', offset=offset, timeout=1, allowed_updates=json.dumps(['message', 'callback_query']))
    except Exception as exc:
        log('getUpdates error', short_error(exc))
        return
    for item in data.get('result', []) or []:
        update_id = item.get('update_id')
        if isinstance(update_id, int):
            set_tg_offset(update_id + 1)
        cb = item.get('callback_query')
        if cb:
            handle_callback_query(cb)
        msg = item.get('message')
        if msg:
            handle_message(msg)


def site_state_summary_map(cfg, state):
    result = {}
    for idx, site in enumerate(cfg.get('sites', [])):
        site_state = state.get('sites', {}).get(state_key_for_site(site), {})
        seen = site_state.get('seen', []) if isinstance(site_state.get('seen'), list) else []
        result[idx] = {
            'seen_count': len(seen),
            'last_seen_at': site_state.get('last_seen_at', ''),
            'last_error': site_state.get('last_error', ''),
        }
    return result


def run_poll_cycle(trigger='manual'):
    if not poll_lock.acquire(blocking=False):
        return '已有轮询正在进行'
    try:
        cfg = load_config()
        state = load_state()
        enabled_sites = [site for site in cfg['sites'] if site.get('enabled') and site.get('rss_url')]
        if not enabled_sites:
            state['meta']['last_poll_at'] = now_text()
            state['meta']['last_poll_summary'] = '没有启用的站点'
            save_state(state)
            log('poll skipped', trigger, 'no enabled sites')
            return '没有启用的站点'

        summary_bits = []
        total_new = 0
        total_fetched = 0
        total_skipped_episode_notify = 0
        total_errors = 0
        per_site = []
        for site in enabled_sites:
            site_name = site.get('name') or site.get('rss_url')
            key = state_key_for_site(site)
            site_state = state['sites'].get(key, {})
            if not isinstance(site_state, dict):
                site_state = {}
            seen = site_state.get('seen', []) if isinstance(site_state.get('seen'), list) else []
            known = set(seen)
            try:
                preview = preview_site_feed(site)
                if not preview['ok']:
                    site_state['last_error'] = preview.get('error') or f'HTTP {preview.get("status_code")}'
                    site_state['last_seen_at'] = now_text()
                    state['sites'][key] = site_state
                    summary_bits.append(f'{site_name}:失败')
                    total_errors += 1
                    per_site.append({
                        'site_name': site_name,
                        'status': 'failed',
                        'fetched': 0,
                        'new': 0,
                        'skipped_episode_notify': 0,
                        'error': site_state['last_error'],
                    })
                    log('poll site failed', site_name, site_state['last_error'])
                    continue

                items = preview.get('items', [])
                total_fetched += len(items)
                keys = [dedupe_key(item) for item in items]
                if not seen:
                    site_state['seen'] = keys[:500]
                    site_state['last_error'] = ''
                    site_state['last_seen_at'] = now_text()
                    site_state['last_item_count'] = len(items)
                    state['sites'][key] = site_state
                    summary_bits.append(f'{site_name}:首次建缓存{len(items)}条')
                    per_site.append({
                        'site_name': site_name,
                        'status': 'seeded',
                        'fetched': len(items),
                        'new': 0,
                        'skipped_episode_notify': 0,
                        'error': '',
                    })
                    log('poll site seeded baseline', site_name, f'items={len(items)}')
                    continue

                new_items = []
                for item in items:
                    item_key = dedupe_key(item)
                    if item_key not in known:
                        new_items.append(item)
                        known.add(item_key)

                site_state['seen'] = ([dedupe_key(item) for item in items] + seen)[:500]
                site_state['last_error'] = ''
                site_state['last_seen_at'] = now_text()
                site_state['last_item_count'] = len(items)
                site_state['last_new_count'] = len(new_items)
                skip_episode_notify = not site.get('push_episodes', True)
                skipped_episode_notify = 0
                skipped_old_notify = 0
                notify_items = []
                for item in new_items:
                    if not is_recent_item(item, 24 * 3600):
                        skipped_old_notify += 1
                        continue
                    if skip_episode_notify and is_episode_item(item):
                        skipped_episode_notify += 1
                        continue
                    notify_items.append(item)
                site_state['last_skipped_episode_notify'] = skipped_episode_notify
                site_state['last_skipped_old_notify'] = skipped_old_notify
                if new_items:
                    site_state['last_new_items'] = [compact_item(item) for item in new_items[:20]]
                    notified = 0
                    for item in reversed(notify_items[:10]):
                        item_title = str(item.get('title') or '').strip()
                        item_link = str(item.get('link') or '').strip()
                        log('notify item start', site_name, item_title[:160], item_link)
                        detail_id = remember_entry(state, site_name, item)
                        detail_html = fetch_detail_html(site, item_link)
                        media_links = extract_media_links(detail_html)
                        poster_urls = [
                            url for url in extract_poster_candidates(detail_html, site.get('base_url', ''))
                            if 'trans.gif' not in url.lower() and 'donate.gif' not in url.lower() and 'chdbits.png' not in url.lower()
                        ]
                        if not poster_urls:
                            poster_urls = fetch_page_poster(media_links.get('imdb')) or fetch_page_poster(media_links.get('douban'))
                        result = send_telegram_message(
                            cfg,
                            build_pretty_telegram_message(site_name, item, detail_html),
                            reply_markup=build_download_keyboard(detail_id, item_link),
                            poster_urls=poster_urls,
                        )
                        if result['ok']:
                            notified += 1
                            log('notify item sent', site_name, item_title[:160], f'poster={bool(poster_urls)}')
                        else:
                            log('telegram notify failed', site_name, item_title[:160], result['message'])

                        if should_auto_dispatch_all_downloaders(site_name, item, detail_html):
                            enabled_downloaders = [dl for dl in cfg.get('downloaders', []) if dl.get('enabled')]
                            download_url = str(item.get('enclosure') or '').strip()
                            if not download_url:
                                log('auto dispatch skipped', site_name, item_title[:160], 'reason=no_enclosure')
                            elif not enabled_downloaders:
                                log('auto dispatch skipped', site_name, item_title[:160], 'reason=no_enabled_downloaders')
                            else:
                                try:
                                    torrent_bytes = download_torrent(download_url, cookie=site.get('cookie', ''))
                                    auto_ok = 0
                                    auto_fail = 0
                                    for downloader in enabled_downloaders:
                                        try:
                                            qb_add_torrent(
                                                downloader,
                                                torrent_bytes,
                                                rename=f'{detail_id}.torrent',
                                                upload_limit_kib=13000,
                                            )
                                            auto_ok += 1
                                            log('auto dispatch sent', site_name, item_title[:160], downloader.get('name') or downloader.get('url') or 'downloader', 'upload_limit_kib=13000')
                                        except Exception as exc:
                                            auto_fail += 1
                                            log('auto dispatch failed', site_name, item_title[:160], downloader.get('name') or downloader.get('url') or 'downloader', short_error(exc))
                                    try:
                                        send_telegram_message(
                                            cfg,
                                            short_text(f'FRDS free 自动派发完成\n主标题：{item_title or "(无标题)"}\n成功下载器：{auto_ok}\n失败下载器：{auto_fail}\n上传限速：13000 kB/s', 900),
                                        )
                                    except Exception as exc:
                                        log('auto dispatch telegram summary failed', site_name, item_title[:160], short_error(exc))
                                except Exception as exc:
                                    log('auto dispatch fetch torrent failed', site_name, item_title[:160], short_error(exc))
                    site_state['last_notify_count'] = notified
                else:
                    site_state['last_notify_count'] = 0
                state['sites'][key] = site_state
                total_new += len(new_items)
                total_skipped_episode_notify += skipped_episode_notify
                per_site.append({
                    'site_name': site_name,
                    'status': 'success',
                    'fetched': len(items),
                    'new': len(new_items),
                    'skipped_episode_notify': skipped_episode_notify + skipped_old_notify,
                    'error': '',
                })
                if skipped_episode_notify and skipped_old_notify:
                    summary_bits.append(f'{site_name}:新增{len(new_items)}条/超24h未推{skipped_old_notify}条/分集未推{skipped_episode_notify}条')
                elif skipped_old_notify:
                    summary_bits.append(f'{site_name}:新增{len(new_items)}条/超24h未推{skipped_old_notify}条')
                elif skipped_episode_notify:
                    summary_bits.append(f'{site_name}:新增{len(new_items)}条/分集未推{skipped_episode_notify}条')
                else:
                    summary_bits.append(f'{site_name}:新增{len(new_items)}条')
                log('poll site success', site_name, f'items={len(items)}', f'new={len(new_items)}', f'skipped_old_notify={skipped_old_notify}', f'skipped_episode_notify={skipped_episode_notify}')
            except Exception as exc:
                site_state['last_error'] = short_error(exc)
                site_state['last_seen_at'] = now_text()
                state['sites'][key] = site_state
                summary_bits.append(f'{site_name}:异常')
                total_errors += 1
                per_site.append({
                    'site_name': site_name,
                    'status': 'error',
                    'fetched': 0,
                    'new': 0,
                    'skipped_episode_notify': 0,
                    'error': short_error(exc),
                })
                log('poll site exception', site_name, short_error(exc))

        state['meta']['last_poll_at'] = now_text()
        summary_text = build_poll_summary_message(trigger, per_site, total_fetched, total_new, total_skipped_episode_notify, total_errors)
        state['meta']['last_poll_summary'] = summary_text
        save_state(state)
        log('poll cycle done', trigger, f'total_new={total_new}', summary_text)
        result = send_telegram_message(cfg, summary_text)
        if result['ok']:
            log('poll summary telegram sent', trigger, f'fetched={total_fetched}', f'new={total_new}', f'skipped_episode={total_skipped_episode_notify}', f'errors={total_errors}')
        else:
            log('poll summary telegram failed', trigger, result['message'])
        return state['meta']['last_poll_summary']
    finally:
        poll_lock.release()


def poll_loop():
    log('poll loop started')
    last_cycle_at = None
    while True:
        try:
            cfg = load_config()
            seconds = max(10, int(cfg['base'].get('poll_seconds', 300)))
            now = monotonic()
            if last_cycle_at is None or (now - last_cycle_at) >= seconds:
                run_poll_cycle(trigger='auto')
                last_cycle_at = monotonic()
                continue
            sleep(min(1, max(0.2, seconds - (now - last_cycle_at))))
        except Exception as exc:
            log('poll loop exception', short_error(exc))
            sleep(5)


def telegram_loop():
    log('telegram loop started')
    while True:
        try:
            poll_telegram_once()
        except Exception as exc:
            log('telegram loop exception', short_error(exc))
        sleep(2)


def qb_login_url(base_url):
    return urljoin(base_url.rstrip('/') + '/', 'api/v2/auth/login')


def test_downloader_connectivity(dl):
    if (dl.get('type') or 'qbittorrent') != 'qbittorrent':
        return {'ok': False, 'message': f'暂不支持该下载器类型测试：{dl.get("type") or "(empty)"}'}
    session = requests.Session()
    session.headers.update({'User-Agent': 'PTRSS/0.1'})
    resp = session.post(
        qb_login_url(dl['url']),
        data={'username': dl.get('username', ''), 'password': dl.get('password', '')},
        timeout=15,
        allow_redirects=True,
    )
    text = (resp.text or '').strip()
    ok = resp.ok and text.lower().startswith('ok')
    if ok:
        return {'ok': True, 'message': f'qBittorrent 登录成功：HTTP {resp.status_code}'}
    return {'ok': False, 'message': f'qBittorrent 登录失败：HTTP {resp.status_code} | 响应: {text[:180] or "(empty)"}'}


def test_telegram_connectivity(cfg):
    token = cfg['telegram'].get('bot_token', '').strip()
    chat_id = cfg['telegram'].get('chat_id', '').strip()
    if not token or not chat_id:
        return {'ok': False, 'message': 'Telegram 测试失败：Bot Token 或 Chat ID 为空'}
    url = f'https://api.telegram.org/bot{token}/sendMessage'
    resp = requests.post(
        url,
        data={'chat_id': chat_id, 'text': 'PTRSS 连通性测试消息：Telegram 推送链路当前可达。'},
        timeout=15,
    )
    try:
        data = resp.json()
    except Exception:
        data = None
    ok = resp.ok and isinstance(data, dict) and data.get('ok') is True
    if ok:
        return {'ok': True, 'message': 'Telegram 测试成功：已发送测试消息'}
    return {'ok': False, 'message': f'Telegram 测试失败：HTTP {resp.status_code} | 响应: {str(data)[:220] if data is not None else (resp.text or "(empty)")[:220]}'}


@app.get('/go/<detail_id>')
def go_detail(detail_id):
    state = load_state()
    entry = state.get('entry_map', {}).get(detail_id)
    if not entry or not entry.get('link'):
        return 'detail not found', 404
    return redirect(entry.get('link'))


@app.get('/')
def root():
    return redirect(url_for('logs_page'))


@app.get('/config')
def config_page():
    cfg = load_config()
    state = load_state()
    return render_template_string(
        CONFIG_PAGE,
        active_tab='config',
        cfg=cfg,
        meta=state.get('meta', {}),
        site_states=site_state_summary_map(cfg, state),
        message=request.args.get('msg', '').strip(),
    )


@app.get('/site/new')
def site_new_page():
    cfg = load_config()
    return render_template_string(
        EDIT_SITE_PAGE,
        active_tab='config',
        cfg=cfg,
        message=request.args.get('msg', '').strip(),
        site=None,
        edit_site_idx=-1,
        is_edit=False,
    )


@app.get('/site/<int:idx>/edit')
def site_edit_page(idx):
    cfg = load_config()
    site = get_item_by_index(cfg['sites'], idx)
    if site is None:
        return redirect(url_for('config_page', msg='站点索引不存在'))
    return render_template_string(
        EDIT_SITE_PAGE,
        active_tab='config',
        cfg=cfg,
        message=request.args.get('msg', '').strip(),
        site=site,
        edit_site_idx=idx,
        is_edit=True,
    )


@app.get('/downloader/new')
def downloader_new_page():
    cfg = load_config()
    return render_template_string(
        EDIT_DOWNLOADER_PAGE,
        active_tab='config',
        cfg=cfg,
        message=request.args.get('msg', '').strip(),
        dl=None,
        edit_dl_idx=-1,
        is_edit=False,
    )


@app.get('/downloader/<int:idx>/edit')
def downloader_edit_page(idx):
    cfg = load_config()
    dl = get_item_by_index(cfg['downloaders'], idx)
    if dl is None:
        return redirect(url_for('config_page', msg='下载器索引不存在'))
    return render_template_string(
        EDIT_DOWNLOADER_PAGE,
        active_tab='config',
        cfg=cfg,
        message=request.args.get('msg', '').strip(),
        dl=dl,
        edit_dl_idx=idx,
        is_edit=True,
    )


@app.get('/logs')
def logs_page():
    cfg = load_config()
    state = load_state()
    lines_raw = (request.args.get('lines') or '300').strip()
    try:
        lines = max(50, min(2000, int(lines_raw)))
    except ValueError:
        lines = 300
    return render_template_string(
        LOGS_PAGE,
        active_tab='logs',
        cfg=cfg,
        meta=state.get('meta', {}),
        logs=read_logs(lines),
        lines=lines,
        message=request.args.get('msg', '').strip(),
    )


@app.get('/logs/raw')
def logs_raw():
    lines_raw = (request.args.get('lines') or '300').strip()
    try:
        lines = max(50, min(5000, int(lines_raw)))
    except ValueError:
        lines = 300
    return read_logs(lines), 200, {'Content-Type': 'text/plain; charset=utf-8'}


@app.post('/save-base')
def save_base():
    cfg = load_config()
    old_base = cfg.get('base', {}) if isinstance(cfg.get('base'), dict) else {}

    submitted_timezone = request.form.get('timezone', '').strip()
    if submitted_timezone:
        cfg['base']['timezone'] = submitted_timezone
    else:
        cfg['base']['timezone'] = old_base.get('timezone', 'Asia/Shanghai') or 'Asia/Shanghai'

    poll_seconds_raw = request.form.get('poll_seconds', '').strip()
    if poll_seconds_raw:
        try:
            cfg['base']['poll_seconds'] = max(10, int(poll_seconds_raw))
        except ValueError:
            cfg['base']['poll_seconds'] = old_base.get('poll_seconds', 300) or 300
    else:
        cfg['base']['poll_seconds'] = old_base.get('poll_seconds', 300) or 300

    save_config(cfg)
    log('saved base config', 'timezone=', cfg['base']['timezone'], 'poll_seconds=', cfg['base']['poll_seconds'])
    return json_or_redirect('基础配置已保存')


@app.post('/run-poll')
def run_poll():
    summary = run_poll_cycle(trigger='manual-web')
    return redirect(url_for('config_page', msg=f'轮询完成：{summary}'))


@app.post('/save-telegram')
def save_telegram():
    cfg = load_config()
    old_telegram = cfg.get('telegram', {}) if isinstance(cfg.get('telegram'), dict) else {}

    submitted_bot_token = request.form.get('bot_token', '').strip()
    submitted_chat_id = request.form.get('chat_id', '').strip()

    cfg['telegram']['bot_token'] = submitted_bot_token or old_telegram.get('bot_token', '')
    cfg['telegram']['chat_id'] = submitted_chat_id or old_telegram.get('chat_id', '')
    cfg['telegram']['enabled'] = bool(request.form.get('enabled'))
    save_config(cfg)
    log('saved telegram config', 'enabled=', cfg['telegram']['enabled'], 'chat_id=', cfg['telegram']['chat_id'] or '(empty)', 'token_preserved=' if old_telegram.get('bot_token') and not submitted_bot_token else 'token_updated=')
    msg = 'Telegram 配置已保存'
    if cfg['telegram']['enabled'] and cfg['telegram']['bot_token'] and cfg['telegram']['chat_id']:
        try:
            result = register_tg_commands(cfg)
            if result.get('ok'):
                msg += '；菜单命令已注册'
        except Exception as exc:
            log('register tg commands failed', short_error(exc))
            msg += f'；但菜单命令注册失败：{short_error(exc)}'
    return json_or_redirect(msg)


@app.post('/test-telegram')
def test_telegram():
    cfg = load_config()
    try:
        result = test_telegram_connectivity(cfg)
        if result.get('ok'):
            try:
                register_tg_commands(cfg)
                result['message'] += '；菜单命令已注册'
            except Exception as exc:
                log('register tg commands failed', short_error(exc))
                result['message'] += f'；菜单命令注册失败：{short_error(exc)}'
        log('tested telegram', 'ok=' if result['ok'] else 'fail=', result['message'])
        return redirect(url_for('config_page', msg=result['message']))
    except Exception as exc:
        msg = f'Telegram 测试异常：{short_error(exc)}'
        log('tested telegram exception', msg)
        return redirect(url_for('config_page', msg=msg))


@app.post('/save-site')
def save_site():
    cfg = load_config()
    edit_idx = parse_index(request.form.get('edit_index', '-1'))
    old_site = get_item_by_index(cfg['sites'], edit_idx) if 0 <= edit_idx < len(cfg['sites']) else None

    submitted_cookie = request.form.get('cookie', '').strip()
    site = normalize_site({
        'name': request.form.get('name', '').strip(),
        'base_url': request.form.get('base_url', '').strip(),
        'rss_url': request.form.get('rss_url', '').strip(),
        'cookie': submitted_cookie,
        'enabled': bool(request.form.get('enabled')),
        'push_episodes': bool(request.form.get('push_episodes')),
    })
    if old_site and not submitted_cookie:
        site['cookie'] = old_site.get('cookie', '')
    if not site['rss_url']:
        if old_site:
            return json_or_redirect('站点 RSS 链接不能为空', ok=False, redirect_endpoint='site_edit_page', redirect_values={'idx': edit_idx})
        return json_or_redirect('站点 RSS 链接不能为空', ok=False, redirect_endpoint='site_new_page')
    if not site['name']:
        site['name'] = urlparse(site['base_url'] or site['rss_url']).netloc or '未命名站点'

    if old_site:
        cfg['sites'][edit_idx] = site
        save_config(cfg)
        log('updated site', site['name'], site['base_url'] or site['rss_url'], 'cookie_preserved=' if old_site.get('cookie') and not submitted_cookie else 'cookie_updated=')
        return json_or_redirect(f'已更新站点：{site["name"]}', redirect_endpoint='site_edit_page', redirect_values={'idx': edit_idx})

    cfg['sites'].append(site)
    save_config(cfg)
    new_idx = len(cfg['sites']) - 1
    log('added site', site['name'], site['base_url'] or site['rss_url'])
    return json_or_redirect(f'已新增站点：{site["name"]}', redirect_endpoint='site_edit_page', redirect_values={'idx': new_idx})


@app.post('/test-site')
def test_site():
    cfg = load_config()
    idx = parse_index(request.form.get('index', '-1'))
    site = get_item_by_index(cfg['sites'], idx)
    if site is None:
        return redirect(url_for('config_page', msg='站点索引不存在'))
    try:
        result = test_site_connectivity(site)
        log('tested site', site.get('name') or idx, result['message'])
        return redirect(url_for('site_edit_page', idx=idx, msg=result['message']))
    except Exception as exc:
        msg = f'站点测试异常：{site.get("name") or idx} | {short_error(exc)}'
        log('tested site exception', msg)
        return redirect(url_for('site_edit_page', idx=idx, msg=msg))


@app.post('/preview-site')
def preview_site():
    cfg = load_config()
    idx = parse_index(request.form.get('index', '-1'))
    site = get_item_by_index(cfg['sites'], idx)
    if site is None:
        return redirect(url_for('config_page', msg='站点索引不存在'))
    try:
        preview = preview_site_feed(site)
        log('previewed site', site.get('name') or idx, f'http={preview.get("status_code")}', f'items={preview.get("item_count")}', f'ok={preview.get("ok")}', preview.get('error') or '')
        preview['items'] = preview.get('items', [])[:10]
        return render_template_string(PREVIEW_PAGE, active_tab='config', cfg=cfg, message='', site=site, preview=preview)
    except Exception as exc:
        msg = f'RSS 预览异常：{site.get("name") or idx} | {short_error(exc)}'
        log('previewed site exception', msg)
        return redirect(url_for('site_edit_page', idx=idx, msg=msg))


@app.post('/delete-site')
def delete_site():
    cfg = load_config()
    state = load_state()
    idx = parse_index(request.form.get('index', '-1'))
    if idx < 0 or idx >= len(cfg['sites']):
        return redirect(url_for('config_page', msg='站点索引不存在'))
    removed = cfg['sites'].pop(idx)
    save_config(cfg)
    state['sites'].pop(state_key_for_site(removed), None)
    save_state(state)
    log('deleted site', removed.get('name') or '未命名站点', removed.get('base_url') or removed.get('rss_url') or '')
    return redirect(url_for('config_page', msg=f'已删除站点：{removed.get("name") or "未命名站点"}'))


@app.post('/save-downloader')
def save_downloader():
    cfg = load_config()
    edit_idx = parse_index(request.form.get('edit_index', '-1'))
    old_item = get_item_by_index(cfg['downloaders'], edit_idx) if 0 <= edit_idx < len(cfg['downloaders']) else None

    submitted_password = request.form.get('password', '').strip()
    item = normalize_downloader({
        'name': request.form.get('name', '').strip(),
        'type': request.form.get('type', 'qbittorrent').strip(),
        'url': request.form.get('url', '').strip(),
        'username': request.form.get('username', '').strip(),
        'password': submitted_password,
        'category': request.form.get('category', '').strip(),
        'tags': request.form.get('tags', '').strip(),
        'savepath': request.form.get('savepath', '').strip(),
        'enabled': bool(request.form.get('enabled')),
    })
    if old_item and not submitted_password:
        item['password'] = old_item.get('password', '')
    if not item['url']:
        if old_item:
            return json_or_redirect('下载器地址不能为空', ok=False, redirect_endpoint='downloader_edit_page', redirect_values={'idx': edit_idx})
        return json_or_redirect('下载器地址不能为空', ok=False, redirect_endpoint='downloader_new_page')
    if not item['name']:
        item['name'] = f'{item["type"]}-downloader'

    if old_item:
        cfg['downloaders'][edit_idx] = item
        save_config(cfg)
        log('updated downloader', item['name'], item['type'], item['url'], 'password_preserved=' if old_item.get('password') and not submitted_password else 'password_updated=')
        return json_or_redirect(f'已更新下载器：{item["name"]}', redirect_endpoint='downloader_edit_page', redirect_values={'idx': edit_idx})

    cfg['downloaders'].append(item)
    save_config(cfg)
    new_idx = len(cfg['downloaders']) - 1
    log('added downloader', item['name'], item['type'], item['url'])
    return json_or_redirect(f'已新增下载器：{item["name"]}', redirect_endpoint='downloader_edit_page', redirect_values={'idx': new_idx})


@app.post('/test-downloader')
def test_downloader():
    cfg = load_config()
    idx = parse_index(request.form.get('index', '-1'))
    dl = get_item_by_index(cfg['downloaders'], idx)
    if dl is None:
        return redirect(url_for('config_page', msg='下载器索引不存在'))
    try:
        result = test_downloader_connectivity(dl)
        log('tested downloader', dl.get('name') or idx, result['message'])
        return redirect(url_for('downloader_edit_page', idx=idx, msg=result['message']))
    except Exception as exc:
        msg = f'下载器测试异常：{dl.get("name") or idx} | {short_error(exc)}'
        log('tested downloader exception', msg)
        return redirect(url_for('downloader_edit_page', idx=idx, msg=msg))


@app.post('/delete-downloader')
def delete_downloader():
    cfg = load_config()
    idx = parse_index(request.form.get('index', '-1'))
    if idx < 0 or idx >= len(cfg['downloaders']):
        return redirect(url_for('config_page', msg='下载器索引不存在'))
    removed = cfg['downloaders'].pop(idx)
    save_config(cfg)
    log('deleted downloader', removed.get('name') or '未命名下载器', removed.get('url') or '')
    return redirect(url_for('config_page', msg=f'已删除下载器：{removed.get("name") or "未命名下载器"}'))


if __name__ == '__main__':
    cfg = load_config()
    load_state()
    try:
        if cfg.get('telegram', {}).get('enabled') and cfg.get('telegram', {}).get('bot_token') and cfg.get('telegram', {}).get('chat_id'):
            register_tg_commands(cfg)
            log('telegram commands registered')
    except Exception as exc:
        log('telegram commands register failed', short_error(exc))
    log('ptrss started', 'port=', PORT)
    threading.Thread(target=poll_loop, daemon=True).start()
    threading.Thread(target=telegram_loop, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT)
