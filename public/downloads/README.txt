Built agent binaries go here.

After building the agent (see ../../agent/build.ps1), copy the produced
audiobus-agent.exe into this folder so the /share page can offer it as a download:

    agent/dist/audiobus-agent.exe  ->  public/downloads/audiobus-agent.exe

The .exe is intentionally git-ignored (it is large and is a build artifact).
Place it here before running `wrangler deploy` so it is published with the site.
