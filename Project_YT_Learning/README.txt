YouTube Knowledge & Investment Immersion — Phase 0.1

Purpose
- Observe captions currently rendered visibly in the YouTube player.
- Show the observed text and approximate playback timestamp in Chrome's Side Panel.
- This is NOT a full transcript extractor and does not fetch caption-track files.

Requirements
- Google Chrome 114 or newer
- Windows 10/11
- Internet access and a YouTube video page

Install
1. Extract this ZIP to a normal folder, e.g. Desktop\YouTube-Knowledge-Extension-0.1.
2. In Chrome, open chrome://extensions
3. Enable Developer mode.
4. Click "Load unpacked".
5. Select the extracted folder containing manifest.json.
6. Open a YouTube video page and click the extension toolbar icon to open the side panel.
7. Turn on CC in the YouTube player and play the video.

Test
- Test an ordinary public video with creator-uploaded captions.
- Test an ordinary public video with auto-generated captions.
- Test a video with captions disabled, and a video without captions.
- Record whether text appears, whether it is duplicated, and whether timestamps are close to the player time.

Limitations
- The prototype reads .ytp-caption-segment elements currently rendered in the player.
- It only observes captions while the extension content script is active; it cannot recover earlier captions.
- Caption display timing may differ from the exact start/end timing of the underlying caption cue.
- YouTube may change its page structure at any time.
- This prototype does not use YouTube Data API, does not call an LLM, and does not download caption files.
- Technical success does not establish permission for commercial or public distribution. Review YouTube Terms and Developer Policies before productizing.

Troubleshooting
- If the panel says it cannot connect, reload the YouTube tab and reopen the panel.
- Make sure the video is on youtube.com/watch?v=...
- Turn on CC and ensure captions are visibly appearing on the video.
- After editing files, open chrome://extensions and click the reload icon for this extension, then refresh the YouTube tab.
