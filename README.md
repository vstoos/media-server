# media-server

A tiny read-only video server for your home network: browse your video folders from a phone,
tablet or another computer and play them in the browser, **HDR included** (HLG / HEVC files play in
HDR in Safari on iPhone, iPad and Mac, and in Edge / Chrome on Windows with HEVC hardware decoding).

- One Python file, standard library only: no install, no database, no transcoding.
- Every subfolder is a page; videos and audio files (mp3, m4a, wav, flac, ogg, opus, aac) get a player
  and a download link; seeking works (HTTP Range).
- Read-only: nothing can be uploaded, changed or deleted, and only video files inside the folders
  you configure are reachable (paths are resolved and checked against the root).
- UI in Ukrainian or English, light and dark theme, phone-friendly.

> Українською: маленький сервер для перегляду своїх відео з телефона чи іншого комп'ютера в домашній
> мережі або через Tailscale, з підтримкою HDR. Тільки читання, нічого не можна змінити чи видалити.

## Maintenance

This is a personal tool shared as-is. **I may not have time to review pull requests or answer
issues.** Please fork or clone it and adapt it to your needs; ideas and wishes are welcome as issues,
with no promise of a reply.

## Run

```bash
python media_server.py                       # folders from config.json, else ~/Videos
python media_server.py D:/Videos E:/Footage  # or name the folders
python media_server.py --port 8765 --bind 0.0.0.0 --lang en
```

Then open `http://<this-computer's-address>:8765` on the other device. On Windows, run it without
a console window with `pythonw media_server.py` (it then logs to `media_server.log`).

Allow the port through the firewall for your private network, or reach the computer over a private
VPN such as [Tailscale](https://tailscale.com). **Do not expose it to the internet: there is no login.**

## config.json (optional)

Copy `config.example.json` to `config.json`:

```json
{
  "folders": [
    {"title": "Videos", "path": "C:/Users/me/Videos"},
    {"title": "Work renders", "path": "D:/projects/film", "key": "work"}
  ],
  "hide": "_raw[.]|^x_",
  "lang": "en",
  "port": 8765
}
```

- `folders`: what to serve; `title` is shown, `key` (optional) is the URL prefix.
- `hide`: regular expression for file and folder names to leave out (temporary renders etc.).
- per folder, `only`: regular expression a file name must match (e.g. `"[.]mp4$"` for finished films
  only), and `"flat": true` to hide its subfolders. Handy for a "Finished films" entry next to the
  full folder tree.

## Start with Windows (optional)

Create a scheduled task that runs at logon:

```powershell
schtasks /Create /TN "media-server" /SC ONLOGON /RL LIMITED /TR "\"C:\Program Files\Python311\pythonw.exe\" C:\path\to\media_server.py"
```

## Test

`python test_server.py` starts the server on a temporary folder and checks browsing, range requests
and that files outside the folders, and non-video files, are refused.

## Licence

MIT, see LICENSE.
