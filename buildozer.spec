[app]
title = MikroTik Voucher Manager
package.name = mtvouchermanager
package.domain = org.example
source.dir = .
source.include_exts = py,png,jpg,kv,json
version = 0.1
requirements = python3,kivy

orientation = portrait
fullscreen = 0

# Crucial permission for connecting to network router
android.permissions = INTERNET

[buildozer]
log_level = 2
warn_on_root = 1
