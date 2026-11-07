# Установка BIS в Yandex Cloud

> Нужен только для доступа сотрудников из любого места. На своём компьютере с Windows — без виртуальной машины,
> Docker и прав администратора: откройте PowerShell и выполните
> `irm https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/install-windows.ps1 | iex`
> (ставит Python и BIS со встроенной базой, создаёт ярлык «BIS» на рабочем столе).

Около 15 минут. Понадобятся: доступ к консоли Yandex Cloud, секретный ключ AI Studio (`AQVN…`) и идентификатор
каталога (`b1g…`). Сервисному аккаунту ключа нужны роли `ai.languageModels.user`, `ai.speechkit-stt.user`,
`ai.vision.user` в этом каталоге.

## 1. Виртуальная машина

Консоль → **Compute Cloud** → **Создать ВМ**:

| Параметр | Значение |
|---|---|
| Образ | Ubuntu 24.04 LTS (или 22.04) |
| Зона | любая (ru-central1-a) |
| vCPU / память | 2 vCPU, 8 ГБ (для 20+ пользователей — 4 vCPU, 16 ГБ) |
| Диск | SSD 50 ГБ |
| Сеть | публичный IP-адрес — автоматически |
| Доступ | логин, например `bis`, и ваш SSH-ключ |

В группе безопасности сети разрешите входящие порты **22** (SSH) и **80** (веб).

Нет SSH-ключа? На своём компьютере: `ssh-keygen -t ed25519`, содержимое файла `~/.ssh/id_ed25519.pub` вставьте в поле ключа.

## 2. Установка

Подключитесь к машине (адрес — на странице ВМ):

```bash
ssh bis@<публичный-IP>
```

И выполните:

```bash
curl -fsSL https://raw.githubusercontent.com/baboevazamatkz-create/bis/claude/construction-ai-automation-system-lya9lz/deploy/install.sh -o install.sh
sudo bash install.sh
```

Скрипт спросит ключ AI Studio (ввод скрыт), каталог, токен Telegram-бота (можно пропустить), затем — загрузить
демо-данные или создать вашу организацию и администратора. Первая сборка занимает 5–10 минут.

В конце появится адрес: `http://<публичный-IP>` — это рабочий BIS для сотрудников.

## 3. После установки

- **Проверить ИИ:** в BIS «ИИ-специалисты» → «Проверить модели».
- **Telegram:** каждый сотрудник — «Настройки» → «Подключить мой Telegram» → команда `/start <код>` боту.
- **Обновить BIS:** `sudo bash /opt/bis/deploy/install.sh` (настройки и данные сохраняются).
- **Настройки** (ключи, токены): `/opt/bis/deploy/.env`, после правки — `cd /opt/bis/deploy && sudo docker compose up -d`.
- **Резервная копия базы:** `cd /opt/bis/deploy && sudo docker compose exec db pg_dump -U bis bis > bis-$(date +%F).sql`.

## Домен и HTTPS (по желанию)

Для адреса вида `bis.компания.ru` и защищённого соединения: направьте A-запись домена на IP машины —
затем можно поставить Caddy перед BIS (автоматический сертификат Let's Encrypt). Это включается отдельно.
