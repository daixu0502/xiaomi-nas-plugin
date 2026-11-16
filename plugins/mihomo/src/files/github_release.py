"""Bounded, verified GitHub release metadata without the REST API quota."""
from html.parser import HTMLParser
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

REPOSITORIES = ('MetaCubeX/mihomo', 'abcfy2/aria2-static-build', 'userdocs/qbittorrent-nox-static')


def allowed_url(url):
    value = urllib.parse.urlsplit(url)
    if value.scheme != 'https' or value.username or value.password or value.port not in (None, 443):
        raise ValueError('发布地址无效')
    if value.hostname == 'github.com':
        if not any(value.path.startswith('/' + repo + '/releases/') for repo in REPOSITORIES):
            raise ValueError('发布地址不属于指定的上游仓库')
    elif value.hostname not in ('release-assets.githubusercontent.com', 'objects.githubusercontent.com'):
        raise ValueError('发布下载重定向不受信任')
    return url


class Redirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def opener(proxy):
    if proxy and (not isinstance(proxy, str) or not re.fullmatch(r'http://127\.0\.0\.1:[0-9]+', proxy)
                  or not 1 <= urllib.parse.urlsplit(proxy).port <= 65535):
        raise ValueError('Mihomo 代理地址无效')
    return urllib.request.build_opener(urllib.request.ProxyHandler({'http': proxy, 'https': proxy} if proxy else {}), Redirect())


def request(url, proxy, maximum, method='GET'):
    req = urllib.request.Request(allowed_url(url), method=method,
                                 headers={'User-Agent': 'Xiaomi-NAS-Release-Reader', 'Accept': '*/*'})
    try:
        with opener(proxy).open(req, timeout=25) as response:
            allowed_url(response.url)
            data = response.read(maximum + 1) if method != 'HEAD' else b''
            if len(data) > maximum:
                raise ValueError('发布响应超过大小限制')
            return data, response.url, response.headers
    except urllib.error.HTTPError as error:
        if error.code in (403, 429) and error.headers.get('X-RateLimit-Remaining') == '0':
            raise ValueError('当前出口的 GitHub 请求额度已用尽，请稍后重试或切换代理节点') from None
        raise ValueError('官方发布源返回 HTTP %d，请检查代理节点或网络' % error.code) from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ValueError('无法连接官方发布源，请检查当前代理或网络') from None


def fetch(url, proxy, maximum):
    return request(url, proxy, maximum)[0]


def latest_tag(repository, proxy):
    if repository not in REPOSITORIES:
        raise ValueError('不支持的发布仓库')
    _, url, _ = request('https://github.com/' + repository + '/releases/latest', proxy, 0, 'HEAD')
    prefix = 'https://github.com/' + repository + '/releases/tag/'
    tag = url[len(prefix):] if url.startswith(prefix) else ''
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', tag):
        raise ValueError('官方最新稳定版跳转无效')
    return tag


class AssetDigest(HTMLParser):
    def __init__(self, path, name):
        super().__init__(convert_charrefs=True)
        self.path, self.name, self.links, self.digests = path, name, 0, []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'a' and attrs.get('href') == self.path:
            self.links += 1
        if tag == 'clipboard-copy' and attrs.get('aria-label') == 'Copy to clipboard digest for ' + self.name:
            self.digests.append(attrs.get('value', ''))


def asset(repository, tag, name, proxy, maximum):
    if repository not in REPOSITORIES or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,95}', tag):
        raise ValueError('发布标签无效')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,160}', name):
        raise ValueError('发布文件名无效')
    path = '/' + repository + '/releases/download/' + tag + '/' + name
    page = fetch('https://github.com/' + repository + '/releases/expanded_assets/' + tag, proxy, 2 * 1024 * 1024)
    parser = AssetDigest(path, name)
    parser.feed(page.decode('utf-8'))
    if parser.links != 1 or len(parser.digests) != 1 or not re.fullmatch(r'sha256:[a-fA-F0-9]{64}', parser.digests[0]):
        raise ValueError('官方发布页未提供唯一的核心文件和 SHA-256，已拒绝更新')
    _, _, headers = request('https://github.com' + path, proxy, 0, 'HEAD')
    length = headers.get('Content-Length', '')
    if not length.isdigit() or not 0 < int(length) <= maximum:
        raise ValueError('无法确认官方核心文件大小或文件过大')
    return dict(name=name, digest=parser.digests[0].lower(), size=int(length), browser_download_url='https://github.com' + path)


def mihomo(proxy):
    tag = latest_tag('MetaCubeX/mihomo', proxy)
    if not re.fullmatch(r'v\d+\.\d+\.\d+', tag):
        raise ValueError('Mihomo 最新稳定版标签无效')
    item = asset('MetaCubeX/mihomo', tag, 'mihomo-linux-arm64-' + tag + '.gz', proxy, 100 * 1024 * 1024)
    return dict(tag_name=tag, assets=[item])


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2:
            raise ValueError('核心发布查询参数无效')
        print(json.dumps(mihomo(sys.argv[1] or False)))
    except Exception as error:
        print(str(error) if isinstance(error, ValueError) else '无法读取核心发布信息', file=sys.stderr)
        raise SystemExit(1)
