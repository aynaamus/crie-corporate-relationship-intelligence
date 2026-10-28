#!/bin/zsh
# Copy the deployment files into a new directory: the repository's files
# (tracked, or new and not ignored by .gitignore) minus deploy/exclude.txt.
# Nothing is deleted from this repository; nothing is pushed.
# usage: zsh deploy/package.zsh <new-directory>
set -eu
REPO=${0:A:h:h}
TARGET=${1:?usage: zsh deploy/package.zsh <new-directory>}
if [[ -e $TARGET ]]; then print -u2 "Refused: $TARGET exists."; exit 1; fi
cd $REPO
LIST=$(mktemp)
{ git ls-files --cached; git ls-files --others --exclude-standard; } | sort -u \
    | grep -vEf <(grep -vE '^(#|$)' deploy/exclude.txt) \
    | while read -r file; do [[ -f $file ]] && print -r -- $file; done > $LIST
mkdir -p $TARGET
rsync -a --files-from=$LIST . $TARGET/
print "Copied $(wc -l < $LIST | tr -d ' ') files to $TARGET."
rm -f $LIST
