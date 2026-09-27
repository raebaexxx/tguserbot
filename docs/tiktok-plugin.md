# TikTok plugin

The `tiktok` plugin downloads a public TikTok video with `yt-dlp`, sends it to
the same chat, and deletes the command message after a successful upload.

## Usage

From the owner account:

```text
/ub tt https://www.tiktok.com/@user/video/123456789
```

The direct form is also supported:

```text
/tt https://vm.tiktok.com/short-link/
```

The command message is edited in place while the file is downloading, showing
percentage, downloaded/total bytes, speed, and ETA. After a successful upload it
is deleted. On failure it remains as an error status so the reason is not lost.

Progress edits are throttled to at most one every three seconds, and the
progress reporter is stopped before the upload begins. An earlier version
skipped that throttle once the percentage reached 100 and only stopped the
reporter after the upload, which produced roughly two `edit_message` calls per
second for the whole upload and overwrote the final status.

## Limits

The plugin accepts only one HTTPS URL whose host is `tiktok.com` or
`tiktokv.com`. Playlists, private/authenticated content, DRM bypass, and
watermark-removal features are not implemented. Downloads are serialized per
plugin instance and sent through the core rate limiter. The size limit is
enforced *during* the download, so an oversized video is abandoned rather than
filled to disk first.

The plugin uses `yt-dlp` with `curl-cffi` for browser impersonation. TikTok
serves a challenge page to clients that do not look like a browser, which is
why `curl-cffi` is a hard dependency. It also uses `ffmpeg` when yt-dlp needs
to merge formats; install `ffmpeg` if downloads fail with a merge error.

## Configuration

Defaults live in `plugins/tiktok/plugin.toml` and can be overridden in
`<data_dir>/plugin-config.toml`:

```toml
[tiktok]
max_file_mib = 100
allowed_domains = ["tiktok.com", "tiktokv.com"]
```

`max_file_mib` must be an integer and `allowed_domains` a list of strings; an
override of the wrong type is refused when the plugin loads rather than failing
mid-download. Widening `allowed_domains` lets yt-dlp fetch from hosts you have
not reviewed — only do that deliberately.

## A note on yt-dlp's plugin scanner

yt-dlp can load third-party extractor plugins from installed packages. The
library entry point this project uses never invokes that scanner
(`load_all_plugins()` is CLI-only), so no `YTDLP_NO_PLUGINS` environment
variable or `plugin_dirs` option is set here. An earlier version set both: the
environment variable from a worker thread, which was a process-wide side effect
on every other plugin, and the option with the wrong type. Neither had any
effect on this code path.

## Safety

Use this only for videos you own or have permission to download and send. Do
not use it for bulk unsolicited forwarding or content you are not allowed to
repost.
