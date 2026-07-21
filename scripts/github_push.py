#!/usr/bin/env python3
"""
通过 GitHub API 更新 api/data.json
绕过 GITHUB_TOKEN 无法 git push 的限制
"""
import json
import base64
import urllib.request
import os

def api(method, path, data=None):
    """GitHub API 封装"""
    token = os.environ.get('GITHUB_TOKEN', '')
    repo = os.environ.get('REPO', 'xiaoliu-bot/market-monitor')
    url = f"https://api.github.com{path}"
    
    headers = {
        'Authorization': f'token {token}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28'
    }
    
    req = urllib.request.Request(url, headers=headers, method=method)
    if data:
        body = json.dumps(data).encode('utf-8')
        req.data = body
        req.add_header('Content-Type', 'application/json')
    
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {'error': e.read().decode('utf-8', errors='replace'), 'code': e.code}

def main():
    repo = os.environ.get('REPO', 'xiaoliu-bot/market-monitor')
    print(f"仓库: {repo}")
    
    # 1. 读取生成的 data.json
    try:
        with open('api/data.json', 'r', encoding='utf-8') as f:
            content = f.read()
        print(f"✅ 读取 data.json 成功 ({len(content)} bytes)")
    except FileNotFoundError:
        print("❌ api/data.json 未找到，跳过更新")
        return
    
    # 2. 获取当前文件 SHA
    result = api('GET', f'/repos/{repo}/contents/api/data.json')
    sha = None
    if 'sha' in result:
        sha = result['sha']
        print(f"  当前 SHA: {sha[:8]}...")
    elif 'error' in result:
        if '404' in str(result.get('error', '')):
            print("  文件不存在，将创建新文件")
        else:
            print(f"  ⚠️ 获取SHA失败: {result['error']}")
    
    # 3. 写入新内容
    encoded = base64.b64encode(content.encode('utf-8')).decode('ascii')
    payload = {
        'message': f"📊 自动更新市场数据 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}",
        'content': encoded,
    }
    if sha:
        payload['sha'] = sha
    
    # 4. 上传
    result = api('PUT', f'/repos/{repo}/contents/api/data.json', payload)
    
    if 'error' in result:
        print(f"❌ 写入失败: {result['error']}")
        exit(1)
    else:
        print(f"✅ 写入成功!")
        print(f"   新 SHA: {result['content']['sha'][:8]}...")

if __name__ == '__main__':
    main()
