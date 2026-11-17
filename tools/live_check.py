#!/usr/bin/env python3
"""
Ask the real GitHub API what the updater would install for the built-in addon list.

Run by the CI build as an informational step (it never fails the build), or by hand:

    python tools/live_check.py [Folder=owner/repo ...] [--search "QUERY" ...]

Set GITHUB_TOKEN to avoid the 60-requests-per-hour anonymous allowance.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import addon_updater as au  # noqa: E402


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass
    args = sys.argv[1:]
    searches, pairs = [], [(repo.split("/")[1], repo) for repo in au.KNOWN_REPOS.values()]
    while args:
        arg = args.pop(0)
        if arg == "--search" and args:
            searches.append(args.pop(0))
        elif "=" in arg:
            folder, repo = arg.split("=", 1)
            pairs.append((folder, repo))
    client = au.GitHub(os.environ.get("GITHUB_TOKEN", ""))
    flavor = os.environ.get("WAU_FLAVOR", "forever")
    for folder, repo in pairs:
        try:
            releases = client.releases(repo)
        except au.GitHubError as exc:
            print("\n== %s -> %s: ERROR %s" % (folder, repo, exc))
            continue
        print("\n== %s -> %s: %d releases returned" % (folder, repo, len(releases)))
        for rel in releases[:4]:
            flags = "".join(f for f, on in ((" [pre-release]", rel.get("prerelease")), (" [draft]", rel.get("draft"))) if on)
            print("   %s%s  %s" % (rel.get("tag_name"), flags, rel.get("published_at")))
            for asset in rel.get("assets") or []:
                print("      - %s (%s bytes%s)" % (asset.get("name"), asset.get("size"),
                                                   ", digest" if asset.get("digest") else ""))
                if (asset.get("name") or "").lower() == "release.json" and rel is releases[0]:
                    print("        manifest: %s" % str(client.manifest(asset.get("browser_download_url") or ""))[:900])
        found = au.pick_release(releases, flavor, folder, False, client)
        if found:
            print("   PICK for %s: %s -> %s (chosen by %s)" % (flavor, found[0].get("tag_name"), found[1].get("name"), found[2]))
        else:
            print("   PICK for %s: nothing suitable" % flavor)
    for query in searches:
        print("\n== search: %s" % query)
        try:
            data = client._api("/search/repositories", {"q": query, "per_page": 10})
        except au.GitHubError as exc:
            print("   ERROR %s" % exc)
            continue
        for item in data.get("items") or []:
            print("   %s  stars=%s pushed=%s  %s" % (item.get("full_name"), item.get("stargazers_count"),
                                                   (item.get("pushed_at") or "")[:10], (item.get("description") or "")[:110]))
    print("\nAPI requests left this hour: %s" % client.remaining)


if __name__ == "__main__":
    main()
