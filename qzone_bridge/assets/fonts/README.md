Bundled render fonts
====================

The publish-card renderer uses Alibaba PuHuiTi 3.0 for cleaner Chinese text.
Only the two required TTF weights are bundled:

- `AlibabaPuHuiTi-3-55-Regular.ttf` for post body and metadata text.
- `AlibabaPuHuiTi-3-75-SemiBold.ttf` for nickname/title text.

Source:

- Official font site: https://www.alibabafonts.com/
- CDN package used for these files: https://www.jsdelivr.com/package/npm/@fontpkg/alibaba-pu-hui-ti-3-0

The renderer falls back to system CJK fonts if these files are missing.
