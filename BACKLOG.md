# Backlog

- **Light version not tested on a real iPhone yet.** Tested 16.11.2026 in Chromium only (browser pane):
  plays 15 s after the click while the rest is made, 2 min buffer, a simulated 47 s outage (TCP proxy
  dropping all connections) played through from the buffer and refilled after. On iOS 17.1+ hls.js uses
  ManagedMediaSource (the video tag has disableremoteplayback for it); older iOS falls back to native HLS.
- **A drop at the very first playlist load** is handled by reloading the source every 3 s (the
  manifestLoadPolicy retries did not fire in the test), but that path itself was not re-tested after the fix.
- **hls.js comes from cdn.jsdelivr.net** at play time. Serving a local copy would make the player work
  on a network without internet access (pure LAN) too.
- **Speed.** About 2-4x real time for 1080p HLG HEVC (CPU HEVC decode is the limit; -hwaccel cuda /
  vulkan gave no gain with libplacebo, measured 16.11.2026). Two people asking for two different films
  queue one after the other.
- **The original (HDR) player** still has only the browser's own buffering; for a bad connection use
  the light version.
