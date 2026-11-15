---
worth: yes
added: 2026-11-15
---
# Роут файлов Kaiten, которым пользуется taskboard, отключат 2027-02-02

`PUT /api/latest/cards/{id}/files` на kaiten.x5.ru отвечает с `Deprecation` и
`Sunset: Tue, 02 Feb 2027` (спайк 2026-11-15, карточка 3920127). Файл получает ссылку
`kaiten-files.x5.ru/<uuid>`, которая открывается без авторизации. Защищённый
`POST /cards/{uid}/files` на X5 пока отдаёт 404. До даты отключения проверить, появился ли
uid-роут, и перевести `upload_plan` на него. Иначе загрузка планов в карточки перестанет
работать. Миграция: https://developers.kaiten.ru/restricted-access-files-migration
