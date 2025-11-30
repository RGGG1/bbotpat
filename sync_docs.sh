#!/bin/bash

# 1) Sync all docs → website
rsync -av --delete /root/bbotpat/docs/ /var/www/bbotpat/

# 2) ALSO sync long-term historical file so ranges work properly
cp /root/bbotpat/dom_mc_history.json /var/www/bbotpat/dom_mc_history.json

# Sync site files to nginx webroot
rsync -av /root/bbotpat/docs/ /var/www/bbotpat/
