#!/usr/bin/env python3
"""Read-only rCTF v1/v2 challenge archiver (Python 3).

- Supports public/no-login mode and optional team-token login.
- Reads challenge lists from /api/v2/challs, with /api/v1/challs fallback.
- Concurrent multi-threaded challenge archiver and attachment downloader.
- Automatic output directory naming based on target domain.
- Incremental/resume downloads (skips files if size matches, --overwrite to force).
- Extracts and downloads attachment URLs from challenge descriptions.
- Advanced challenge filtering (--category, --chall, --solved, --unsolved, --min-solves, --max-solves).
- Never submits flags or calls challenge-instance/admin endpoints.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

USER_AGENT = "rctf-public-archive/1.0"
REDIRECT_CODES = {301, 302, 303, 307, 308}
COMMON_EXTENSIONS = {
    "zip", "tar", "gz", "bz2", "xz", "7z", "rar", "tgz",
    "pdf", "png", "jpg", "jpeg", "gif", "bmp", "svg", "webp",
    "txt", "py", "pyc", "c", "cpp", "h", "java", "sh", "asm", "s",
    "bin", "elf", "exe", "apk", "iso", "raw", "dat", "nes",
    "json", "toml", "yaml", "yml", "wasm", "whl", "pcap", "pcapng",
    "patch", "diff", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "csv",
    "mp3", "wav", "mp4", "rom", "gba", "gb"
}
FILE_KEYWORDS = {"download", "handout", "attachment", "file", "archive"}

print_lock = threading.Lock()


def safe_print(*args, **kwargs) -> None:
    with print_lock:
        print(*args, **kwargs)


def root_url(value: str) -> str:
    value = value.strip()
    if not value.startswith(("http://", "https://")):
        value = "https://" + value
    p = urlsplit(value)
    if not p.netloc:
        raise ValueError(f"URL target tidak valid: {value}")
    path = p.path.rstrip("/")
    if path in {"/challenges", "/scores", "/login", "/register"}:
        path = ""
    return f"{p.scheme}://{p.netloc}{path}".rstrip("/")


def safe_name(value: str, fallback: str = "item") -> str:
    value = Path(str(value)).name
    value = re.sub(r"[^\w.() -]+", "_", value, flags=re.UNICODE).strip(" .")
    return (value[:140] or fallback)


def default_output_dir(base: str) -> str:
    p = urlsplit(base)
    host = (p.hostname or p.netloc or "ctf_dump").split(":")[0].strip()
    return safe_name(host, "ctf_dump")


def api_json(session: requests.Session, url: str, timeout: float):
    try:
        response = session.get(url, timeout=timeout, headers={"Accept": "application/json"})
    except requests.RequestException as exc:
        return None, None, str(exc)
    if response.status_code != 200:
        return None, response.status_code, f"HTTP {response.status_code}"
    try:
        return response.json(), response.status_code, None
    except ValueError:
        return None, response.status_code, "respons bukan JSON"


def login_with_team_token(session: requests.Session, base: str, token: str, timeout: float) -> None:
    token = unquote(token.strip())
    if not token:
        raise RuntimeError("Team token kosong.")
    try:
        response = session.post(
            base + "/api/v1/auth/login",
            json={"teamToken": token},
            headers={"Accept": "application/json"},
            timeout=timeout,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Gagal menghubungi endpoint login: {exc}") from exc
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    auth_token = (payload.get("data") or {}).get("authToken") if isinstance(payload, dict) else None
    if response.status_code != 200 or not auth_token:
        kind = payload.get("kind", "") if isinstance(payload, dict) else ""
        raise RuntimeError(f"Login gagal (HTTP {response.status_code}, {kind or 'respons tidak valid'}).")
    session.headers.update({"Authorization": f"Bearer {auth_token}"})


def fetch_user_solves(session: requests.Session, base: str, timeout: float) -> set[str]:
    payload, status, error = api_json(session, base + "/api/v1/users/me", timeout)
    if isinstance(payload, dict) and payload.get("kind") == "goodUserData":
        solves = (payload.get("data") or {}).get("solves") or []
        return {str(s.get("id")) for s in solves if isinstance(s, dict) and s.get("id")}
    return set()


def fetch_challenges(session: requests.Session, base: str, timeout: float):
    errors = []
    for version, expected_kind in (("v2", "goodChallengesV2"), ("v1", "goodChallenges")):
        endpoint = f"/api/{version}/challs"
        payload, status, error = api_json(session, base + endpoint, timeout)
        if isinstance(payload, dict) and payload.get("kind") == expected_kind and isinstance(payload.get("data"), list):
            return version, endpoint, payload["data"]
        detail = error or (f"jenis respons {payload.get('kind')!r}" if isinstance(payload, dict) else "format tidak dikenal")
        errors.append(f"{endpoint}: {detail}")
    raise RuntimeError("Tidak bisa mengambil challenge list. " + "; ".join(errors))


def filter_challenges(challenges: list[dict], args, user_solves: set[str] | None = None) -> list[dict]:
    result = list(challenges)

    # Filter Category
    if args.category:
        cat_patterns = [c.strip() for c in args.category.split(",") if c.strip()]
        filtered = []
        for c in result:
            cat = str(c.get("category") or "")
            if any(p.lower() in cat.lower() or re.search(p, cat, re.I) for p in cat_patterns):
                filtered.append(c)
        result = filtered

    # Filter Challenge Name / ID
    if args.chall:
        pattern = args.chall.strip()
        filtered = []
        for c in result:
            name = str(c.get("name") or "")
            cid = str(c.get("id") or "")
            if (pattern.lower() in name.lower() or pattern.lower() in cid.lower() or
                    re.search(pattern, name, re.I) or re.search(pattern, cid, re.I)):
                filtered.append(c)
        result = filtered

    # Filter Solved / Unsolved (requires login)
    if args.solved:
        solves_set = user_solves or set()
        result = [c for c in result if str(c.get("id")) in solves_set]
    elif args.unsolved:
        solves_set = user_solves or set()
        result = [c for c in result if str(c.get("id")) not in solves_set]

    # Filter Solves count (global)
    if args.min_solves is not None:
        result = [c for c in result if isinstance(c.get("solves"), (int, float)) and c["solves"] >= args.min_solves]
    if args.max_solves is not None:
        result = [c for c in result if isinstance(c.get("solves"), (int, float)) and c["solves"] <= args.max_solves]

    return result


def same_origin(left: str, right: str) -> bool:
    def parts(url):
        p = urlsplit(url)
        scheme = p.scheme.lower()
        port = p.port or (443 if scheme == "https" else 80 if scheme == "http" else None)
        return scheme, (p.hostname or "").lower(), port
    return parts(left) == parts(right)


def extract_filename_from_url_path(url: str) -> str | None:
    path = urlsplit(url).path
    segments = [s for s in path.strip("/").split("/") if s]
    for seg in reversed(segments):
        if seg.lower() in {"download", "view", "raw", "preview", "edit"}:
            continue
        m = re.search(r"\.([a-zA-Z0-9]{1,8})$", seg)
        if m and m.group(1).lower() in COMMON_EXTENSIONS:
            return seg
    return segments[-1] if segments else None


def is_likely_file(label: str, url: str) -> bool:
    if url.startswith("/uploads/") or "/uploads/" in url:
        return True
    path = urlsplit(url).path
    # Label extension
    m_lbl = re.search(r"\.([a-zA-Z0-9]{1,8})$", label.strip())
    if m_lbl and m_lbl.group(1).lower() in COMMON_EXTENSIONS:
        return True
    # URL path extension
    segments = [s for s in path.strip("/").split("/") if s]
    for seg in reversed(segments):
        if seg.lower() in {"download", "view", "raw", "preview"}:
            continue
        m_seg = re.search(r"\.([a-zA-Z0-9]{1,8})$", seg)
        if m_seg and m_seg.group(1).lower() in COMMON_EXTENSIONS:
            return True
    if "drive.google.com/file/d/" in url:
        return True
    if any(k in label.lower() for k in FILE_KEYWORDS):
        return True
    if path.endswith("/download"):
        return True
    return False


def extract_description_files(description: str, base_url: str, existing_files: list,
                              allow_all: bool = False) -> list[dict]:
    if not description:
        return []

    known_urls = set()
    for f in existing_files:
        if isinstance(f, dict) and f.get("url"):
            u = str(f["url"]).strip()
            known_urls.add(u)
            known_urls.add(urljoin(base_url.rstrip("/") + "/", u))
        elif isinstance(f, str):
            known_urls.add(f.strip())
            known_urls.add(urljoin(base_url.rstrip("/") + "/", f.strip()))

    found = []

    # 1. Markdown links: [label](url)
    for m in re.finditer(r"\[([^\]]+)\]\((https?://[^\s)]+|/[^\s)]+)\)", description, re.DOTALL):
        raw_label = re.sub(r"\s+", " ", m.group(1)).strip()
        raw_url = m.group(2).strip().rstrip(".,;:)]")
        abs_url = urljoin(base_url.rstrip("/") + "/", raw_url)
        if raw_url in known_urls or abs_url in known_urls:
            continue
        if not allow_all and not is_likely_file(raw_label, raw_url):
            continue

        known_urls.add(raw_url)
        known_urls.add(abs_url)

        # Tentukan nama file
        m_ext = re.search(r"\.[a-zA-Z0-9]{1,8}$", raw_label)
        if m_ext:
            file_name = safe_name(raw_label)
        else:
            path_fn = extract_filename_from_url_path(raw_url)
            if path_fn and re.search(r"\.[a-zA-Z0-9]{1,8}$", path_fn):
                file_name = safe_name(path_fn)
            else:
                file_name = safe_name(raw_label or path_fn or "attachment")

        found.append({"name": file_name, "url": raw_url, "from_description": True})

    # 2. Bare URLs
    for m in re.finditer(r"(?:https?://|/uploads/)[^\s)\]\"\'<>]+", description):
        raw_url = m.group(0).strip().rstrip(".,;:)]")
        abs_url = urljoin(base_url.rstrip("/") + "/", raw_url)
        if raw_url in known_urls or abs_url in known_urls:
            continue
        if not allow_all and not is_likely_file("", raw_url):
            continue

        known_urls.add(raw_url)
        known_urls.add(abs_url)

        path_fn = extract_filename_from_url_path(raw_url) or "attachment"
        file_name = safe_name(path_fn)
        found.append({"name": file_name, "url": raw_url, "from_description": True})

    return found


def download_file(session: requests.Session, base: str, remote_url: str, destination: Path,
                  max_bytes: int, timeout: float, allow_external: bool,
                  declared_size: int | None = None, overwrite: bool = False) -> tuple[bool, str]:
    # Pengecekan awal incremental/resume sebelum network call jika declared_size cocok
    if destination.exists() and not overwrite:
        local_size = destination.stat().st_size
        if declared_size is not None and declared_size > 0:
            if local_size == declared_size:
                return True, f"{local_size} bytes (cached)"
        elif local_size > 0:
            pass

    current = urljoin(base.rstrip("/") + "/", remote_url)
    response = None
    for _ in range(6):
        if not allow_external and not same_origin(base, current):
            return False, "host eksternal dilewati (pakai --allow-external-files jika memang diizinkan)"
        try:
            response = session.get(current, stream=True, allow_redirects=False, timeout=timeout)
        except requests.RequestException as exc:
            return False, str(exc)
        if response.status_code in REDIRECT_CODES:
            location = response.headers.get("Location")
            response.close()
            if not location:
                return False, "redirect tidak memiliki Location"
            current = urljoin(current, location)
            continue
        break
    else:
        return False, "redirect terlalu banyak"

    if response is None:
        return False, "tidak ada respons"
    if response.status_code != 200:
        status = response.status_code
        response.close()
        return False, f"HTTP {status}"

    content_length = response.headers.get("Content-Length")
    cl_int = int(content_length) if (content_length and content_length.isdigit()) else None

    # Batas ukuran maksimum
    if cl_int and cl_int > max_bytes:
        response.close()
        return False, f"ukuran melewati batas ({cl_int} bytes)"

    # Resume check dengan Content-Length
    if destination.exists() and not overwrite:
        local_size = destination.stat().st_size
        if cl_int is not None and local_size == cl_int:
            response.close()
            return True, f"{local_size} bytes (cached)"
        elif cl_int is None and local_size > 0 and declared_size is None:
            response.close()
            return True, f"{local_size} bytes (cached)"

    temp = destination.with_name(destination.name + ".part")
    written = 0
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("wb") as out:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    temp.unlink(missing_ok=True)
                    return False, "stream melewati batas ukuran"
                out.write(chunk)
        temp.replace(destination)
        return True, f"{written} bytes"
    except (OSError, requests.RequestException) as exc:
        temp.unlink(missing_ok=True)
        return False, str(exc)
    finally:
        response.close()


def write_challenge(base: str, category_dir: Path, challenge: dict, session: requests.Session,
                    args, tag_prefix: str = "") -> tuple[int, int]:
    category = str(challenge.get("category") or "Uncategorized")
    name = str(challenge.get("name") or challenge.get("id") or "Untitled")
    cid = str(challenge.get("id") or name)
    folder = category_dir / safe_name(category, "Uncategorized") / f"{safe_name(name, 'challenge')}_{safe_name(cid, 'id')[:48]}"
    folder.mkdir(parents=True, exist_ok=True)

    desc = str(challenge.get("description") or "")
    api_files = list(challenge.get("files") or [])

    # Ekstraksi file lampiran dari body description (jika tidak dinonaktifkan)
    desc_files = []
    if not getattr(args, "no_desc_files", False):
        desc_files = extract_description_files(desc, base, api_files, allow_all=getattr(args, "all_desc_links", False))

    # Tulis challenge.json
    dump_data = dict(challenge)
    if desc_files:
        dump_data["description_files"] = desc_files
    (folder / "challenge.json").write_text(json.dumps(dump_data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    lines = [f"# {name}", "", f"- **Category:** {category}"]
    if challenge.get("author"):
        lines.append(f"- **Author:** {challenge['author']}")
    if challenge.get("points") is not None:
        lines.append(f"- **Points:** {challenge['points']}")
    if challenge.get("solves") is not None:
        lines.append(f"- **Solves:** {challenge['solves']}")
    lines.extend(["", desc, ""])

    all_files = []
    for item in api_files:
        if isinstance(item, str):
            all_files.append({"url": item, "name": Path(urlsplit(item).path).name or "attachment", "declared_size": None, "source": "api"})
        elif isinstance(item, dict) and item.get("url"):
            all_files.append({
                "url": str(item["url"]),
                "name": str(item.get("name") or Path(urlsplit(str(item["url"])).path).name or "attachment"),
                "declared_size": item.get("size"),
                "source": "api"
            })
    for item in desc_files:
        all_files.append({"url": item["url"], "name": item["name"], "declared_size": None, "source": "desc"})

    ok_count = skipped_count = 0
    if all_files:
        lines.extend(["## Attachments", ""])
        for item in all_files:
            remote = item["url"]
            src_tag = "[desc] " if item.get("source") == "desc" else ""
            file_name = safe_name(item["name"], "attachment")
            absolute = urljoin(base.rstrip("/") + "/", remote)
            declared_size = item.get("declared_size")

            if args.no_files:
                lines.append(f"- [{file_name}]({absolute}) — tidak diunduh (--no-files)")
                skipped_count += 1
            elif isinstance(declared_size, (int, float)) and declared_size > args.max_file_mb * 1024 * 1024:
                lines.append(f"- [{file_name}]({absolute}) — dilewati (melewati batas ukuran)")
                skipped_count += 1
            else:
                success, info = download_file(
                    session, base, remote, folder / file_name,
                    args.max_file_mb * 1024 * 1024, args.timeout,
                    args.allow_external_files,
                    declared_size=declared_size if isinstance(declared_size, (int, float)) else None,
                    overwrite=args.overwrite
                )
                if success:
                    status_lbl = "CACHE" if "cached" in info else "OK"
                    safe_print(f"  [{status_lbl}] {tag_prefix}{src_tag}{file_name} ({info})")
                    lines.append(f"- [{file_name}](./{file_name})")
                    ok_count += 1
                else:
                    safe_print(f"  [SKIP] {tag_prefix}{src_tag}{file_name}: {info}", file=sys.stderr)
                    lines.append(f"- [{file_name}]({absolute}) — gagal/lewati: {info}")
                    skipped_count += 1
                if args.delay > 0:
                    time.sleep(args.delay)
        lines.append("")

    (folder / "README.md").write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return ok_count, skipped_count


class DownloadStats:
    def __init__(self):
        self.lock = threading.Lock()
        self.completed = 0
        self.files_ok = 0
        self.files_skipped = 0

    def add(self, ok: int, skipped: int) -> int:
        with self.lock:
            self.completed += 1
            self.files_ok += ok
            self.files_skipped += skipped
            return self.completed


def main() -> int:
    parser = argparse.ArgumentParser(description="Dumper read-only challenge rCTF v1/v2 dan lampiran publik")
    parser.add_argument("url", help="Root URL situs CTF, mis. https://ctf.example.com/")
    parser.add_argument("-o", "--output", default=None, help="Folder output (default: nama host/domain dari URL target)")

    # Auth
    auth = parser.add_mutually_exclusive_group()
    auth.add_argument("--login", action="store_true", help="Login normal menggunakan team token; token diminta tersembunyi")
    auth.add_argument("--no-login", action="store_true", help="Jangan login; gunakan akses publik (default)")
    parser.add_argument("--token-env", default="CTF_TEAM_TOKEN", help="Nama environment variable team token (default: CTF_TEAM_TOKEN)")

    # Execution & Concurrency
    parser.add_argument("-t", "--threads", type=int, default=4, help="Jumlah concurrent worker threads (default: 4)")
    parser.add_argument("-f", "--overwrite", "--force", action="store_true", help="Unduh ulang dan timpa file yang sudah ada (abaikan cache)")
    parser.add_argument("--list-only", action="store_true", help="Cetak jumlah dan nama challenge saja; tidak menyimpan/unduh")
    parser.add_argument("--no-files", action="store_true", help="Simpan README dan JSON saja, jangan unduh lampiran")
    parser.add_argument("--allow-external-files", action="store_true", help="Izinkan host lampiran berbeda; default hanya host situs yang sama")
    parser.add_argument("--no-desc-files", action="store_true", help="Nonaktifkan ekstraksi tautan lampiran dari deskripsi challenge")
    parser.add_argument("--all-desc-links", action="store_true", help="Ekstraksi seluruh URL di deskripsi tanpa filter tipe file")

    # Filters
    parser.add_argument("-c", "--category", help="Filter kategori (case-insensitive substring/regex atau koma, mis. 'web,pwn')")
    parser.add_argument("--chall", "--name", help="Filter nama atau ID challenge (case-insensitive substring/regex)")
    solve_grp = parser.add_mutually_exclusive_group()
    solve_grp.add_argument("--solved", action="store_true", help="Hanya challenge yang sudah di-solve tim (memerlukan --login)")
    solve_grp.add_argument("--unsolved", action="store_true", help="Hanya challenge yang belum di-solve tim (memerlukan --login)")
    parser.add_argument("--min-solves", type=int, help="Filter solve count global minimum")
    parser.add_argument("--max-solves", type=int, help="Filter solve count global maksimum")

    # Limits & Timing
    parser.add_argument("--max-file-mb", type=int, default=200, help="Batas per lampiran dalam MiB (default: 200)")
    parser.add_argument("--delay", type=float, default=0.15, help="Jeda antar unduhan per worker dalam detik (default: 0.15)")
    parser.add_argument("--timeout", type=float, default=30, help="Timeout request dalam detik (default: 30)")

    args = parser.parse_args()

    if args.threads < 1:
        parser.error("--threads harus >= 1")
    if args.max_file_mb < 1 or args.delay < 0:
        parser.error("--max-file-mb harus >= 1 dan --delay harus >= 0")
    if (args.solved or args.unsolved) and not args.login:
        parser.error("--solved dan --unsolved memerlukan mode --login.")

    try:
        base = root_url(args.url)
    except ValueError as exc:
        parser.error(str(exc))

    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    pool_size = max(20, args.threads * 2)
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    user_solves = None
    if args.login:
        token = os.environ.get(args.token_env)
        if not token:
            token = getpass.getpass("rCTF team token (input disembunyikan): ")
        try:
            login_with_team_token(session, base, token, args.timeout)
        except RuntimeError as exc:
            safe_print(f"[error] {exc}", file=sys.stderr)
            return 2
        safe_print("Login berhasil; token tidak dicetak atau disimpan.")
        user_solves = fetch_user_solves(session, base, args.timeout)
    else:
        safe_print("Mode no-login: memakai endpoint challenge yang bisa diakses publik.")

    try:
        version, endpoint, raw_challenges = fetch_challenges(session, base, args.timeout)
    except RuntimeError as exc:
        safe_print(f"[error] {exc}", file=sys.stderr)
        if not args.login:
            safe_print("Coba --login bila endpoint memerlukan sesi akun yang sah.", file=sys.stderr)
        return 2

    safe_print(f"Platform: rCTF {version}; endpoint: {endpoint}")
    safe_print(f"Total challenge di platform: {len(raw_challenges)}")

    challenges = filter_challenges(raw_challenges, args, user_solves)
    if len(challenges) != len(raw_challenges):
        safe_print(f"Challenge setelah filter: {len(challenges)}")

    if not challenges:
        safe_print("Tidak ada challenge yang cocok dengan filter yang ditentukan.")
        return 0

    if args.list_only:
        for c in challenges:
            cat = c.get("category", "Uncategorized")
            name = c.get("name", c.get("id", "Untitled"))
            solves_info = f" ({c['solves']} solves)" if c.get("solves") is not None else ""
            pts_info = f" [{c['points']} pts]" if c.get("points") is not None else ""
            print(f"- [{cat}] {name}{pts_info}{solves_info}")
        return 0

    # Tentukan output folder
    output_dir_str = args.output if args.output else default_output_dir(base)
    out = Path(output_dir_str).expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    safe_print(f"Output folder: {out}")

    # Simpan challenges.json
    (out / "challenges.json").write_text(
        json.dumps({
            "platform": f"rCTF {version}",
            "source": base + endpoint,
            "count": len(challenges),
            "data": challenges
        }, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8"
    )

    stats = DownloadStats()
    total = len(challenges)

    def worker(item: tuple[int, dict]) -> tuple[int, int]:
        idx, chall = item
        name = str(chall.get("name") or chall.get("id") or "Untitled")
        cat = str(chall.get("category") or "Uncategorized")
        tag = f"[{idx}/{total}] " if args.threads == 1 else f"[{cat}] "
        if args.threads == 1:
            safe_print(f"[{idx}/{total}] {name}")
        ok, skipped = write_challenge(base, out, chall, session, args, tag_prefix=tag)
        done_num = stats.add(ok, skipped)
        if args.threads > 1:
            safe_print(f"[{done_num}/{total}] Selesai: [{cat}] {name} ({ok} file OK/cached, {skipped} dilewati)")
        return ok, skipped

    start_time = time.time()
    try:
        if args.threads > 1:
            safe_print(f"Memulai unduhan paralel ({args.threads} threads)...")
            with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as executor:
                futures = [executor.submit(worker, (i, chall)) for i, chall in enumerate(challenges, 1)]
                for future in concurrent.futures.as_completed(futures):
                    future.result()
        else:
            for i, chall in enumerate(challenges, 1):
                worker((i, chall))
    except KeyboardInterrupt:
        safe_print("\n[interrupted] Dihentikan oleh pengguna.", file=sys.stderr)
        return 130

    elapsed = time.time() - start_time
    safe_print(
        f"Selesai dalam {elapsed:.2f}s: {len(challenges)} challenge diproses, "
        f"{stats.files_ok} lampiran terunduh/cached, {stats.files_skipped} dilewati/gagal."
    )
    safe_print(f"Folder arsip: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
