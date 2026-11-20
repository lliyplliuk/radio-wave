"""Радио из коллекции — нативное Linux-приложение (GTK4 + GStreamer + libtorrent).

Пакеты:
  config      — пути и константы
  state       — постоянное состояние приложения (JSON)
  library     — каталог аудио (импорт, метаданные)
  tags        — чтение тегов без внешних зависимостей
  ranker      — ранжирование (чистый Python, без GUI)
  queue       — очередь радио (чистый Python)
  magnet      — разбор magnet-ссылок и .torrent
  torrent     — BitTorrent через libtorrent
  player      — воспроизведение через GStreamer playbin
  catalog    — поиск записей MusicBrainz и похожих треков ListenBrainz
  service     — связка модулей
  ui          — GTK4-интерфейс
"""

__version__ = "0.1.0"
