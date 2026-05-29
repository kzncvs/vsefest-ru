# Фото-галерея с фестиваля

Как устроена кнопка «Фотографии» на `fest-vse.ru`: модалка с выбором
фотографа, листалкой и скачиванием оригиналов.

## Архитектура: три уровня картинок, байты — вне git

Лендинг деплоится через GitHub Pages из git-репозитория, который пушится в
**три** remote'а. Класть сюда сотни тяжёлых фоток нельзя: git хранит историю
навсегда (вес не уходит после удаления), ×3 раздувание, плюс мягкий лимит
GitHub Pages ~100 ГБ трафика/мес.

Поэтому:

| Уровень | Что | Где | Размер |
|---|---|---|---|
| **Миниатюра** | 640px, JPEG q72 — для сетки-плитки | S3, папка `thumb/` | ~50–70 КБ |
| **Превью** | 2000px, JPEG q82 — для листалки/лайтбокса | S3 (Yandex Object Storage) | ~0.4–0.9 МБ |
| **Оригинал** | как прислал фотограф — для скачивания | S3, папка `orig/` | МБ-ы |
| **Манифест** | `assets/web/photos.json` — только метаданные + ссылки | **в git** | ~единицы КБ |

В git попадает только `photos.json`. Сами картинки — в бакете. Листалка
показывает лёгкие превью, «Скачать оригинал» ведёт на полноразмерный файл.

## Манифест `assets/web/photos.json`

```json
{
  "baseUrl": "https://<bucket>.storage.yandexcloud.net",
  "photographers": [
    {
      "slug": "photographer-1",
      "name": "Имя в табе",
      "socials": [
        { "label": "@nickname", "url": "https://t.me/nickname" },
        { "label": "VK", "url": "https://vk.com/nickname" }
      ],
      "photos": [
        { "thumb": "photos/photographer-1/thumb/001.jpg",
          "preview": "photos/photographer-1/001.jpg",
          "original": "photos/photographer-1/orig/001.jpg" }
      ]
    }
  ]
}
```

- `thumb` / `preview` / `original` — ключи **относительно** `baseUrl`. Сменить
  бакет = поменять один `baseUrl`. Модалка открывается сеткой-плиткой (`thumb`),
  клик по плитке → листалка (`preview`), «скачать» → `original`.
- `socials` — список (можно несколько: tg / vk / instagram); каждый рисуется
  отдельной ссылкой рядом с листалкой.
- Фронтенд (`index.html`, IIFE `initGallery`) фетчит этот файл; кнопка
  «Фотографии» появляется, только если есть хотя бы один фотограф с фото.
  Битые ссылки на превью не ломают листалку — слайд показывает заглушку.

## Подготовка и заливка

Скрипт `scripts/build_gallery.py` делает всё: ресайз превью (`sips`, встроен
в macOS), заливку обоих уровней в S3 и генерацию `photos.json`.

```sh
# 1. Положить оригиналы под incoming/<slug>/ — удобно симлинком на папку-источник:
#      ln -sfn "/путь/к/папке фотографа" incoming/<slug>
#    Файлы берутся с ВЕРХНЕГО уровня папки. Если фотки внутри подпапки
#    (напр. .../Photos/) — симлинкуй прямо на неё.
#    (incoming/ и build/ — в .gitignore, в репозиторий не попадают)

# 2. В CONFIG-блоке scripts/build_gallery.py прописать BUCKET и имена/ссылки
#    фотографов (slug должен совпадать с папкой в incoming/).

# 3. Собрать превью + манифест локально, посмотреть результат:
source .venv/bin/activate
python scripts/build_gallery.py
#    → ресайзит в build/previews/, перезаписывает assets/web/photos.json

# 4. Залить превью и оригиналы в бакет:
python scripts/build_gallery.py --upload
```

Нумерация в манифесте (`001.jpg`, `002.jpg`, … — ширина по числу фоток) идёт
по алфавиту имён исходников. Хотите свой порядок — переименуйте файлы в
`incoming/<slug>/` (например префиксами `01_`, `02_`). Превью всегда `.jpg`;
оригинал сохраняет исходное расширение. Скрипт параллельный (8 воркеров) и
кеширует превью: повторный прогон/`--upload` не пережимает их заново.

### Добавить четвёртого фотографа

1. `incoming/photographer-4/` + его фотки.
2. Дописать запись в `PHOTOGRAPHERS` в `scripts/build_gallery.py`.
3. `python scripts/build_gallery.py --upload`.

## Настройка бакета Yandex Object Storage

Сделано один раз через `yc` CLI (можно и в веб-консоли):

1. Бакет `fest-vse-photos`, класс STANDARD, **публичное чтение объектов**
   (листинг закрыт). После этого `<bucket>.storage.yandexcloud.net/<key>`
   отдаётся всем:
   ```sh
   yc storage bucket create --name fest-vse-photos \
      --default-storage-class standard --max-size 10737418240
   yc storage bucket update --name fest-vse-photos --public-read
   ```
2. Сервис-аккаунт + роль на запись + статический ключ → в aws-профиль `festvse`:
   ```sh
   yc iam service-account create --name festvse-photos-sa
   yc resource-manager folder add-access-binding default \
      --role storage.editor --subject serviceAccount:<sa-id>
   yc iam access-key create --service-account-name festvse-photos-sa --format json
   #  key_id + secret из вывода → aws configure set ... --profile festvse (region ru-central1)
   ```
   Скрипт ходит с `--profile festvse` и `--endpoint-url https://storage.yandexcloud.net`
   (зашито в CONFIG). Публичность — на уровне бакета, поэтому per-object ACL не
   ставим (`PUBLIC_ACL = False`).
3. **CORS не нужен**: превью грузятся как `<img src>` (без CORS), манифест —
   с того же домена (`assets/web/photos.json`), скачивание идёт через
   `Content-Disposition`.

### Почему скачивание «просто работает»

Оригиналы заливаются с заголовком `Content-Disposition: attachment;
filename="festvse_<slug>_NN.jpg"`. Браузер по такой ссылке **скачивает** файл
с нормальным именем, а не открывает в табе. Атрибут `download` в HTML для
кросс-доменных ссылок игнорируется — поэтому работает именно заголовок на
объекте, его ставит скрипт при `--upload`.

### Своё имя домена (опционально)

Можно повесить CDN + домен `photos.fest-vse.ru` через Yandex CDN и поменять
`PUBLIC_BASE` в скрипте — манифест перегенерится с новыми ссылками.

## Файлы

| Файл | Роль |
|---|---|
| `index.html` (`.gallery-*`, `initGallery`) | UI: кнопка, модалка, табы, листалка |
| `assets/web/photos.json` | манифест (в git) |
| `scripts/build_gallery.py` | ресайз + заливка в S3 + генерация манифеста |
| `incoming/<slug>/` | исходники от фотографов (локально, gitignored) |
| `build/previews/` | staged превью (локально, gitignored) |
