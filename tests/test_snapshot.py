from monitor.snapshot import (
    CrawlTarget,
    extract_html_text,
    normalize_text,
    parse_sitemap,
    safe_url_slug,
    sha256_text,
)


def test_extract_html_ignores_navigation_and_scripts():
    html = """
    <html><body><nav>Noise</nav><main><h1>UTA</h1><p>Shared collateral.</p></main><script>noise()</script></body></html>
    """
    assert extract_html_text(html) == "UTA\nShared collateral."


def test_normalize_ignores_volatile_last_updated_line():
    text = "Title\nLast updated 2 days ago\nPortfolio margin\nPortfolio margin\n"
    normalized = normalize_text(text, [r"^Last updated .*$"])
    assert normalized == "Title\nPortfolio margin\n"


def test_hash_is_deterministic():
    assert sha256_text("abc\n") == sha256_text("abc\n")


def test_parse_urlset_sitemap():
    xml = """<?xml version='1.0'?><urlset xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <url><loc>https://docs.example/a</loc></url><url><loc>https://docs.example/b</loc></url>
    </urlset>"""
    kind, locs = parse_sitemap(xml)
    assert kind == "urlset"
    assert locs == ["https://docs.example/a", "https://docs.example/b"]


def test_parse_sitemap_index():
    xml = """<sitemapindex xmlns='http://www.sitemaps.org/schemas/sitemap/0.9'>
      <sitemap><loc>https://docs.example/sitemap-1.xml</loc></sitemap>
    </sitemapindex>"""
    kind, locs = parse_sitemap(xml)
    assert kind == "sitemapindex"
    assert locs == ["https://docs.example/sitemap-1.xml"]


def test_safe_url_slug_is_stable_and_distinguishes_paths():
    assert safe_url_slug("https://x.test/a") == safe_url_slug("https://x.test/a")
    assert safe_url_slug("https://x.test/a") != safe_url_slug("https://x.test/b")


def test_same_content_does_not_rewrite_metadata(monkeypatch, tmp_path):
    from monitor import snapshot

    class Response:
        text = "<main><h1>Liquid Perps</h1><p>ERC-4626 collateral.</p></main>"
        headers = {"ETag": "first"}

    target = snapshot.Target(protocol="hpl", slug="liquid", url="https://example.test")
    monkeypatch.setattr(snapshot, "fetch", lambda _url: Response())

    first = snapshot.snapshot_target(target, tmp_path, ())
    meta_path = tmp_path / "pages" / "hpl" / "direct" / "liquid.json"
    first_meta = meta_path.read_text(encoding="utf-8")

    Response.headers = {"ETag": "second"}
    second = snapshot.snapshot_target(target, tmp_path, ())
    second_meta = meta_path.read_text(encoding="utf-8")

    assert first["changed"] is True
    assert second["changed"] is False
    assert first_meta == second_meta


def test_href_only_change_is_preserved():
    old = extract_html_text('<main><a href="/old">API docs</a></main>', 'https://docs.example/')
    new = extract_html_text('<main><a href="/new">API docs</a></main>', 'https://docs.example/')
    assert old != new
    assert 'https://docs.example/old' in old
    assert 'https://docs.example/new' in new


def test_frontend_scripts_are_opt_in():
    html = '<html><body><main>Perps</main><script src="/_next/static/app-abc.js"></script></body></html>'
    normal = extract_html_text(html, 'https://near.com/')
    frontend = extract_html_text(html, 'https://near.com/', include_scripts=True)
    assert 'app-abc.js' not in normal
    assert 'https://near.com/_next/static/app-abc.js' in frontend
