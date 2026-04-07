import os

dir_path = r'H:\CRM brasileiros no atacama\templates'
target = '''                <a href="/docs" class="nav-item" target="_blank">
                    <span class="nav-icon">📖</span>
                    <span>API Docs</span>
                </a>'''
replacement = '''                <a href="/docs" class="nav-item" target="_blank">
                    <span class="nav-icon">📖</span>
                    <span>API Docs</span>
                </a>
                <a href="http://localhost:5678" class="nav-item" target="_blank">
                    <span class="nav-icon">⚡</span>
                    <span>Servidor n8n</span>
                </a>'''

for filename in os.listdir(dir_path):
    if filename.endswith('.html'):
        file_path = os.path.join(dir_path, filename)
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        if target in content:
            new_content = content.replace(target, replacement)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f'Updated {filename}')
        else:
            print(f'Target not found in {filename}')
