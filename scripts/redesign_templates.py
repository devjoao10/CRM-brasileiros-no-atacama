"""
Aplica o redesign visual (sidebar clara, SVG icons) em todos os templates restantes.
Rodar: python scripts/redesign_templates.py
"""
import re, os

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES = os.path.join(BASE, 'templates')

# ── SVG icons ──────────────────────────────────────────────────────────────
ICONS = {
    'dashboard': '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7"/><rect x="14" y="3" width="7" height="7"/><rect x="3" y="14" width="7" height="7"/><rect x="14" y="14" width="7" height="7"/></svg>',
    'ai':        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2a10 10 0 1 0 10 10"/><path d="M12 6v6l4 2"/><circle cx="19" cy="5" r="3"/></svg>',
    'leads':     '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    'tags':      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M20.59 13.41l-7.17 7.17a2 2 0 0 1-2.83 0L2 12V2h10l8.59 8.59a2 2 0 0 1 0 2.82z"/><line x1="7" y1="7" x2="7.01" y2="7"/></svg>',
    'pipeline':  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    'segmentacao':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    'tarefas':   '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 11 12 14 22 4"/><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11"/></svg>',
    'relatorios':'<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="20" x2="18" y2="10"/><line x1="12" y1="20" x2="12" y2="4"/><line x1="6" y1="20" x2="6" y2="14"/></svg>',
    'equipe':    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></svg>',
    'docs':      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/></svg>',
    'n8n':       '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>',
    'logout':    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><polyline points="16 17 21 12 16 7"/><line x1="21" y1="12" x2="9" y2="12"/></svg>',
    'menu':      '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>',
}

# ── New sidebar CSS (replaces old dark sidebar block) ─────────────────────
NEW_SIDEBAR_CSS = """\
        .sidebar { width: 240px; background: var(--sidebar-bg); border-right: 1px solid var(--sidebar-border); display: flex; flex-direction: column; position: fixed; top: 0; left: 0; bottom: 0; z-index: var(--z-sidebar); }
        .sidebar-header { padding: var(--sp-5); display: flex; align-items: center; gap: var(--sp-3); border-bottom: 1px solid var(--sidebar-border); min-height: 56px; }
        .sidebar-logo { width: 32px; height: 32px; border-radius: var(--radius-md); overflow: hidden; flex-shrink: 0; }
        .sidebar-logo img { width: 100%; height: 100%; object-fit: cover; }
        .sidebar-brand { font-size: var(--font-size-sm); font-weight: var(--fw-semibold); color: var(--color-text); line-height: 1.3; }
        .sidebar-brand span { color: var(--color-primary); }
        .sidebar-nav { flex: 1; padding: var(--sp-3) 0; overflow-y: auto; }
        .nav-section { padding: var(--sp-1) var(--sp-4); margin-top: var(--sp-4); }
        .nav-section-title { font-size: 10px; font-weight: var(--fw-semibold); text-transform: uppercase; letter-spacing: 0.1em; color: var(--color-text-4); margin-bottom: var(--sp-1); }
        .nav-item { display: flex; align-items: center; gap: var(--sp-3); padding: 8px var(--sp-4); color: var(--sidebar-text); font-size: var(--font-size-sm); font-weight: var(--fw-medium); transition: background var(--t-fast), color var(--t-fast); cursor: pointer; text-decoration: none; border-left: 2px solid transparent; margin: 1px var(--sp-2); border-radius: var(--radius-md); }
        .nav-item:hover { background: var(--sidebar-hover-bg); color: var(--color-text); }
        .nav-item.active { background: var(--sidebar-active-bg); color: var(--sidebar-active-text); font-weight: var(--fw-semibold); }
        .nav-item .nav-icon { width: 18px; height: 18px; display: flex; align-items: center; justify-content: center; flex-shrink: 0; opacity: 0.7; }
        .nav-item.active .nav-icon { opacity: 1; }
        .nav-item .nav-badge { margin-left: auto; background: var(--color-primary-light); color: var(--color-primary); font-size: var(--font-size-xs); padding: 1px 6px; border-radius: var(--radius-full); font-weight: var(--fw-semibold); }
        .sidebar-footer { padding: var(--sp-3) var(--sp-2); border-top: 1px solid var(--sidebar-border); }
        .main-content { flex: 1; margin-left: 240px; display: flex; flex-direction: column; }
        .top-header { height: 56px; background: var(--color-surface); border-bottom: 1px solid var(--color-border); display: flex; align-items: center; justify-content: space-between; padding: 0 var(--sp-6); position: sticky; top: 0; z-index: 100; }
        .header-left { display: flex; align-items: center; gap: var(--sp-3); }
        .menu-toggle { display: none; background: none; border: none; cursor: pointer; padding: 6px; color: var(--color-text-3); border-radius: var(--radius-md); }
        .menu-toggle:hover { background: var(--color-bg); }
        .page-title { font-size: var(--font-size-base); font-weight: var(--fw-semibold); color: var(--color-text); }
        .header-right { display: flex; align-items: center; gap: var(--sp-3); }
        .user-info { display: flex; align-items: center; gap: var(--sp-2); padding: 5px var(--sp-3); border-radius: var(--radius-md); cursor: pointer; transition: background var(--t-fast); }
        .user-info:hover { background: var(--color-bg); }
        .user-avatar { width: 30px; height: 30px; border-radius: var(--radius-full); background: var(--color-primary); color: #fff; display: flex; align-items: center; justify-content: center; font-weight: var(--fw-semibold); font-size: var(--font-size-xs); }
        .user-name { font-size: var(--font-size-sm); font-weight: var(--fw-medium); color: var(--color-text); }
        .user-role { font-size: var(--font-size-xs); color: var(--color-text-3); }
        .content-area { flex: 1; padding: var(--sp-6) var(--sp-8); background: var(--color-bg); }\
"""

def I(key): return f'<span class="nav-icon">{ICONS[key]}</span>'

def make_nav(active):
    def item(href, key, label, target=''):
        cls = 'nav-item active' if key == active else 'nav-item'
        tgt = f' target="{target}"' if target else ''
        return f'                <a href="{href}" class="{cls}"{tgt}>{I(key)}<span>{label}</span></a>'
    lines = [
        '            <nav class="sidebar-nav">',
        '                <div class="nav-section"><div class="nav-section-title">Principal</div></div>',
        item('/dashboard','dashboard','Dashboard'),
        '                <div class="nav-section"><div class="nav-section-title">Inteligência</div></div>',
        item('/ai','ai','Assistente IA'),
        '                <div class="nav-section"><div class="nav-section-title">CRM</div></div>',
        item('/leads','leads','Leads'),
        item('/tags','tags','Tags'),
        item('/pipeline','pipeline','Pipeline'),
        item('/segmentacao','segmentacao','Segmentação'),
        item('/tarefas','tarefas','Tarefas'),
        '                <div class="nav-section"><div class="nav-section-title">Administração</div></div>',
        item('/relatorios','relatorios','Relatórios'),
        item('/equipe','equipe','Equipe e Usuários'),
        '                <div class="nav-section"><div class="nav-section-title">Sistema</div></div>',
        item('/docs','docs','API Docs','_blank'),
        item('https://n8n.crmbrasileirosnoatacama.cloud','n8n','Servidor n8n','_blank'),
        '            </nav>',
    ]
    return '\n'.join(lines)

def make_sidebar(active):
    return f"""        <aside class="sidebar" id="sidebar">
            <div class="sidebar-header">
                <div class="sidebar-logo"><img src="/static/img/logo.png" alt="Logo"></div>
                <div class="sidebar-brand">Brasileiros <span>no Atacama</span></div>
            </div>
{make_nav(active)}
            <div class="sidebar-footer">
                <a href="#" class="nav-item" id="logoutBtn">{I('logout')}<span>Sair</span></a>
            </div>
        </aside>"""

def make_menu_toggle():
    return f'<button class="menu-toggle" id="menuToggle" aria-label="Menu">{ICONS["menu"]}</button>'

# ── CSS pattern to replace (sidebar block in each template) ───────────────
# Matches from ".sidebar {" up to and including ".content-area { ... }"
CSS_PATTERN = re.compile(
    r'\.sidebar \{ width: 260px;.*?\.content-area \{[^}]+\}',
    re.DOTALL
)

# ── Sidebar HTML pattern ───────────────────────────────────────────────────
SIDEBAR_HTML_PATTERN = re.compile(
    r'<aside class="sidebar"[^>]*>.*?</aside>',
    re.DOTALL
)

# ── Menu toggle ────────────────────────────────────────────────────────────
MENU_TOGGLE_PATTERN = re.compile(
    r'<button class="menu-toggle"[^>]*>.*?</button>',
    re.DOTALL
)

# ── CSS link cache busting ─────────────────────────────────────────────────
CSS_LINK_PATTERN = re.compile(
    r'(href="/static/css/(?:variables|base|components)\.css)(")'
)

# ── Files to process (active page key per template) ───────────────────────
FILES = {
    'leads.html':      'leads',
    'tags.html':       'tags',
    'pipeline.html':   'pipeline',
    'segmentacao.html':'segmentacao',
    'tarefas.html':    'tarefas',
    'relatorios.html': 'relatorios',
    'ai.html':         'ai',
    'equipes.html':    'equipe',
}

# ── Process ────────────────────────────────────────────────────────────────
for fname, active in FILES.items():
    path = os.path.join(TEMPLATES, fname)
    if not os.path.exists(path):
        print(f'SKIP (not found): {fname}')
        continue

    with open(path, encoding='utf-8') as f:
        html = f.read()

    original = html

    # 1. Replace sidebar CSS block
    html = CSS_PATTERN.sub(NEW_SIDEBAR_CSS, html)

    # 2. Replace sidebar HTML
    html = SIDEBAR_HTML_PATTERN.sub(make_sidebar(active), html)

    # 3. Replace menu toggle emoji
    html = MENU_TOGGLE_PATTERN.sub(make_menu_toggle(), html)

    # 4. Cache bust CSS links
    html = CSS_LINK_PATTERN.sub(r'\1?v=2\2', html)

    if html != original:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(html)
        print(f'OK: {fname}')
    else:
        print(f'NO CHANGE: {fname} — check patterns manually')

print('\nDone.')
