# Image Optimization Flow

Как ужимать PNG-картинки в `assets/web/` и HTML-оптимизации,
которые уменьшают вес и ускоряют загрузку `fest-vse.ru` / `vsefest.ru`.

## Зачем

Landing-страница почти полностью состоит из картинок (рисованный логотип
«ВСЕ» + ~27 декоративных дудлов + 4 штампа партнёров). Без оптимизации
начальный вес — ~1.5 MB. После прогона описанного ниже пайплайна — ~615 KB
(-59%) без видимой потери качества.

## Требуемые инструменты

```sh
brew install pngquant oxipng
```

- **pngquant** — lossy сжатие через уменьшение палитры. Для рисованной
  графики (line art, плоские цвета) визуально неотличимо от оригинала,
  выигрыш 50-80%.
- **oxipng** — lossless дожимание PNG. Запускается после pngquant,
  добавляет ещё ~5-15% сверху.

Playwright (для замера реального веса страницы) уже есть в `.venv/`.

## Бэкап перед оптимизацией

**Всегда** делать перед любым прогоном. Оригиналы хранятся в
`assets/.image_backups/` (в `.gitignore`, локально):

```sh
BACKUP_DIR="assets/.image_backups/pre-optim-$(date +%Y-%m-%d)"
mkdir -p "$BACKUP_DIR"
cp assets/web/*.png "$BACKUP_DIR/"
```

Откатиться на оригиналы в любой момент:

```sh
cp assets/.image_backups/pre-optim-YYYY-MM-DD/*.png assets/web/
```

## Пайплайн сжатия

Один проход по всем PNG в `assets/web/`:

```sh
# 1. Lossy palette reduction (pngquant).
#    --quality=75-95 — визуально неотличимо для line art и flat colors.
#    --skip-if-larger — оставить оригинал, если результат больше.
for f in assets/web/*.png; do
  pngquant --quality=75-95 --speed 1 --force --skip-if-larger \
           --output "$f" "$f"
done

# 2. Lossless tail compression (oxipng).
#    -o max — максимальный уровень оптимизации (медленнее, но лучше).
oxipng -o max --quiet assets/web/*.png
```

**Важно**: `pngquant` не принимает несколько input-файлов вместе с `--output {}`,
поэтому нужен per-file цикл. `xargs -n1 pngquant --output {} {}` падает с
"Only one input file is allowed when --output is used".

## HTML-оптимизации для скорости загрузки

Эти правки в `index.html` не уменьшают вес, но существенно ускоряют
первую отрисовку (LCP):

### 1. `defer` на внешних скриптах

```html
<script src="https://cdn.qtickets.tech/openapi.js" defer></script>
```

Без `defer` парсер HTML останавливается на скачивании скрипта. В нашем
случае qtickets-скрипт долго грузится и задерживает `load` event на
30+ секунд. С `defer` — парсинг продолжается, скрипт исполняется после
DOMContentLoaded. Замер: было 30с (таймаут), стало ~950 мс.

### 2. `fetchpriority="high"` + `decoding="async"` на критической картинке

Главный логотип — самый важный визуальный элемент первого экрана. Явно
говорим браузеру поднять его в приоритете очереди загрузки:

```html
<img class="letters-img"
     src="assets/web/hero_letters.png"
     alt="ВСЕ"
     fetchpriority="high"
     decoding="async">
```

`decoding="async"` — декодировать PNG в background thread, не блокировать
main thread.

### 3. `loading="lazy"` + `decoding="async"` на декоративных картинках

Дудлы и partnеrs-mobile — декоративные, не критичные для first paint.
На них:

```html
<img src="assets/web/doodle_XX.png" alt="" loading="lazy" decoding="async">
```

Браузер грузит их в idle-очереди после приоритетных запросов, и на узких
вьюпортах — только когда они приближаются к видимой зоне скролла.

Практический эффект: hero_letters и важный контент отрисовываются
раньше; общий вес тот же, но LCP меньше.

## Ожидаемые результаты

На свежей установке (апрель 2026, 52 PNG в `assets/web/`):

| Метрика | До | После | Δ |
|---|---|---|---|
| Total page weight (1440×900) | 1510 KB | 615 KB | -59% |
| Images только | 1484 KB | 423 KB | -71% |
| `hero_letters.png` | 700 KB | 220 KB | -69% |
| `fomin.png` | 156 KB | 37 KB | -76% |
| `moscow.png` | 69 KB | 17 KB | -76% |
| Типичный дудл | 20-50 KB | 4-15 KB | ~-70% |
| Load event (desktop) | 30 с (timeout) | ~950 мс | блокировка убрана |

## Замер веса страницы

Пайплайн замера (playwright через headless Chromium), чтобы увидеть
реальный network transfer:

```sh
# Server
source .venv/bin/activate
python -m http.server 8765 --bind 127.0.0.1 &

# Measure
python <<'PY'
from playwright.sync_api import sync_playwright
URL = "http://127.0.0.1:8765/index.html"
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1440, "height": 900})
    page = ctx.new_page()
    reqs = []
    def on_response(r):
        try: size = len(r.body())
        except: size = None
        reqs.append({'url': r.url, 'type': r.request.resource_type, 'size': size})
    page.on("response", on_response)
    try:
        page.goto(URL, wait_until="load", timeout=15000)
    except: pass
    page.wait_for_timeout(1500)
    total = sum(r['size'] for r in reqs if r['size'])
    print(f"Total: {total:,} bytes = {total/1024:.1f} KB ({len(reqs)} requests)")
    for r in sorted([x for x in reqs if x['size']], key=lambda x: -x['size'])[:15]:
        print(f"  {r['size']:>8,}  {r['url'].replace('http://127.0.0.1:8765/', '')}")
    b.close()
PY
```

## Что ещё можно улучшить (не сделано)

### WebP через `<picture>`

WebP обычно ещё на 30-50% меньше оптимизированного PNG. Не применено,
потому что требует изменения структуры HTML (`<picture>` + source с
WebP + fallback `<img>` с PNG). Если понадобится — команды:

```sh
for f in assets/web/*.png; do
  cwebp -q 85 -alpha_q 90 "$f" -o "${f%.png}.webp"
done
```

И в HTML:
```html
<picture>
  <source srcset="assets/web/hero_letters.webp" type="image/webp">
  <img src="assets/web/hero_letters.png" alt="ВСЕ" fetchpriority="high">
</picture>
```

WebP поддерживается во всех браузерах с начала 2020-х (Safari 14+).

### Ресайз больших PNG до 2x rendered size

Некоторые файлы чрезмерно большие относительно того, как они
отображаются:

| файл | natural | max rendered | оптимум (2x) |
|---|---|---|---|
| `hero_letters.png` | 1200×1032 | ~800 px wide | 1600×~1376 (уже ок) |
| `fomin.png` | 600×600 | ~140 px wide | 280×280 |
| `moscow.png` | 600×178 | ~200 px wide | 400×118 |
| `smena.png` | 600×151 | ~200 px wide | 400×101 |
| `sha.png` | 640×640 | ~140 px wide | 280×280 |

Ресайз до 2x rendered size + повторный pngquant даст ещё 30-50% на
партнёрах. Не применено, чтобы не трогать исходники без необходимости
(вдруг понадобится показывать на больших дисплеях).

### Минификация inline CSS в `index.html`

Убрать отступы и комментарии из `<style>` блока. Сэкономит ~5-7 KB из
текущих 25 KB HTML. Делается через `cleancss` или `html-minifier-terser`.
Не критично.

## Когда запускать

- После добавления новых PNG в `assets/web/` — прогонять пайплайн по
  новым файлам:
  ```sh
  pngquant --quality=75-95 --force --skip-if-larger \
           --output assets/web/NEW_FILE.png assets/web/NEW_FILE.png
  oxipng -o max --quiet assets/web/NEW_FILE.png
  ```
- После замены существующего PNG — аналогично.
- Периодически (раз в полгода) — `brew upgrade pngquant oxipng` и
  полный прогон по всем файлам, новые версии инструментов могут
  дожать ещё пару процентов.

## История оптимизаций

| дата | действие | экономия |
|---|---|---|
| 2026-04-08 | Первичный pngquant + oxipng по всем PNG в `assets/web/` | 1484 → 423 KB (-71%) |
| 2026-04-08 | `defer` на qtickets, `fetchpriority=high` на logo, `loading=lazy` на доудлах | LCP blocker убран |
