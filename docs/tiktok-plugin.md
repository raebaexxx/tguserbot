# TikTok plugin

The `tiktok` plugin downloads a public TikTok video with `yt-dlp`, sends it to
the same chat, and deletes the command message after the upload attempt.

## Usage

From the owner account:

```text
/ub tiktok https://www.tiktok.com/@user/video/123456789
```

The direct form is also supported:

```text
/tiktok https://vm.tiktok.com/short-link/
```

The plugin accepts only one HTTPS URL whose host is `tiktok.com` or
`tiktokv.com`. Playlists, private/authenticated content, DRM bypass, and
watermark-removal features are not implemented. The maximum download size is
50 MiB. Downloads are serialized per plugin instance and sent through the core
rate limiter.

The plugin uses `yt-dlp` with `curl-cffi` for TikTok browser impersonation and
uses `ffmpeg` when yt-dlp needs to merge formats. The current server already
has ffmpeg installed.

Use this only for videos you own or have permission to download and send. Do
not use it for bulk unsolicited forwarding or content you are not allowed to
repost.
