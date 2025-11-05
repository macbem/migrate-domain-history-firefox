# migrate-domain-history-firefox
Util that allows you to back up and migrate entries if a website you're using frequently has changed its domain.

Replace `OLD_DOMAIN_SUFFIX` and `NEW_DOMAIN_SUFFIX` with desired domain names. Then, run with `python3 migrate.py help`.

tldr;
run - `python3 migrate.py --verbose list-profiles`, see the profile name you need to use. You'll get output like

```
python3 migrate.py --verbose list-profiles
Found profiles:
- /Users/maciej/Library/Application Support/Firefox/Profiles/egujunnf.default [Default=1] (places=0, prefs=0, cookies=0, logins=0); 1 files, 47.0 B
- /Users/maciej/Library/Application Support/Firefox/Profiles/bfttckdr.default-release [default-release] (places=1, prefs=1, cookies=1, logins=1); 19621 files, 2.9 GB
```

Then, run `python3 migrate.py --verbose --profile default-release backup --dest ./backups`. After the backup, give the tool a test drive with `python3 migrate.py --verbose --profile default-release rewrite-all --dry-run` and execute with `python3 migrate.py --verbose --profile default-release rewrite-all`.

100% vibe coded, but tested on my own history and it worked properly. Use with caution.
