# Dumper rCTF v1/v2 — Python 3

Dumper multi-threaded untuk mengarsipkan challenge dan lampiran dari platform rCTF (v1 & v2). Mendukung akun publik (no-login) maupun akun tim (login via team token).

File lampiran (baik yang terdaftar di API maupun yang tertaut di dalam teks deskripsi challenge) otomatis diunduh, metadata dirangkum ke `README.md` dan `challenge.json`, serta seluruh challenge direkam di `challenges.json`.

Script hanya memakai endpoint read/login dan GET untuk lampiran. Script **tidak pernah** submit flag, membuat instance challenge, atau memanggil endpoint admin.

---

## Fitur Utama

- **Otomatisasi Nama Folder**: Default folder output menyesuaikan nama domain/host dari URL (misal: `https://ctf.example.com/` otomatis disimpan ke folder `ctf.example.com/`).
- **Concurrent Worker Pool (`-t` / `--threads`)**: Unduhan paralel multi-thread (default: 4 worker) sehingga proses dump jauh lebih cepat.
- **Resume & Incremental Download**: Melewati lampiran yang sudah terunduh lengkap sebelumnya berdasarkan ukuran file (`[CACHE]`), menghemat bandwidth dan waktu saat proses diulang. Opsi `--overwrite` tersedia untuk unduh ulang paksa.
- **Ekstraksi Lampiran Deskripsi**: Mendeteksi dan mengunduh tautan file atau lampiran yang disematkan author di dalam body teks Markdown deskripsi (termasuk upload relatif dan link file eksternal).
- **Filter Fleksibel**:
  - Filter kategori (`-c`, `--category`): mendukung regex atau nama jamak dipisah koma (misal: `--category web,pwn`).
  - Filter nama/ID challenge (`--chall` / `--name`): pencarian substring atau regex.
  - Filter status solve tim (`--solved` / `--unsolved`): memerlukan `--login`.
  - Filter batas solve global (`--min-solves`, `--max-solves`).

---

## Instalasi

```bash
python3 -m pip install requests
```

---

## Contoh Penggunaan

### 1. Dump Standar (No-login)

Folder output otomatis menjadi `ctf.example.com/`:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login
```

Menentukan folder output manual:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login -o my_ctf_dump
```

### 2. Memeriksa Daftar Challenge (`--list-only`)

Melihat challenge yang tersedia beserta poin dan jumlah solve tanpa mengunduh file apa pun:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login --list-only
```

### 3. Menggunakan Filter

Filter satu atau lebih kategori:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login -c web,pwn
```

Filter berdasarkan nama challenge:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login --chall "Baby"
```

Filter berdasarkan jumlah solve:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login --min-solves 100
```

### 4. Mengatur Thread & Cache (Resume)

Gunakan 8 thread untuk mempercepat unduhan:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login -t 8
```

Jika proses terhenti lalu dijalankan kembali, file yang sudah cocok ukurannya akan otomatis dilewati (`[CACHE]`). Untuk memaksa mengunduh ulang seluruh file:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --no-login -f
```

### 5. Login Menggunakan Team Token

Token diminta secara tersembunyi di terminal:

```bash
python3 rctf_dumper.py https://ctf.example.com/ --login
```

Atau set environment variable sebelum menjalankan:

```bash
read -s -p 'Team token: ' CTF_TEAM_TOKEN; echo
export CTF_TEAM_TOKEN
python3 rctf_dumper.py https://ctf.example.com/ --login
```

Dengan sesi login, filter `--solved` atau `--unsolved` dapat digunakan:

```bash
# Hanya unduh challenge yang belum diselesaikan oleh tim
python3 rctf_dumper.py https://ctf.example.com/ --login --unsolved
```

---

## Daftar Lengkap Opsi CLI

| Argumen | Keterangan |
| :--- | :--- |
| `url` | Root URL situs CTF target (misal: `https://ctf.example.com/`) |
| `-o, --output DIR` | Nama folder tujuan (default: nama host URL, mis. `ctf.example.com`) |
| `-t, --threads N` | Jumlah concurrent worker threads (default: 4) |
| `-f, --overwrite` | Timpa/unduh ulang file yang sudah ada (abaikan cache resume) |
| `-c, --category CAT` | Filter kategori (case-insensitive substring/regex atau koma, misal `web,pwn`) |
| `--chall NAME` | Filter nama atau ID challenge (substring/regex) |
| `--solved` | Hanya dump challenge yang sudah di-solve tim (perlu `--login`) |
| `--unsolved` | Hanya dump challenge yang belum di-solve tim (perlu `--login`) |
| `--min-solves N` | Filter jumlah solve global minimum |
| `--max-solves N` | Filter jumlah solve global maksimum |
| `--no-files` | Simpan `README.md` dan `challenge.json` saja, tanpa mengunduh lampiran |
| `--allow-external-files` | Izinkan unduhan file dari domain luar (default: hanya origin situs yang sama) |
| `--no-desc-files` | Nonaktifkan ekstraksi tautan file dari body teks deskripsi |
| `--all-desc-links` | Ekstraksi semua URL di deskripsi tanpa filter jenis file |
| `--max-file-mb MB` | Batas ukuran per file dalam MiB (default: 200 MiB) |
| `--delay SEC` | Jeda antar-unduhan per worker dalam detik (default: 0.15s) |
| `--timeout SEC` | Timeout request HTTP dalam detik (default: 30s) |
| `--list-only` | Cetak daftar challenge saja, tidak menyimpan atau mengunduh |
| `--login` | Login menggunakan team token |
| `--no-login` | Akses tanpa akun / publik (default) |
| `--token-env VAR` | Nama variabel environment untuk team token (default: `CTF_TEAM_TOKEN`) |
