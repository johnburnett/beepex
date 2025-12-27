"use strict";

const MEDIA = window.MEDIA.map(it => {
  return {
    src: `${window.MEDIA_PREFIX}/${it[0]}`,
    messageId: it[1]
  }
});
const elGalleryGrid = document.getElementById("gallery-grid");
const elSearchText = document.getElementById("search-text");
const elSearchCount = document.getElementById("search-count");

function extOf(path) {
  const i = path.lastIndexOf(".");
  return i >= 0 ? path.slice(i + 1).toLowerCase() : "";
}

function typeOf(path) {
  const e = extOf(path);
  if (["jpg","jpeg","png","gif","webp","bmp","avif"].includes(e)) return "image";
  if (["mp4","webm","m4v","mov","ogv"].includes(e)) return "video";
  if (["ogg","mp3","wav","m4a","flac","opus"].includes(e)) return "audio";
  return "other";
}

function timestampFromPath(path) {
  const filename = path.split("/").pop();
  const re = /^(?<date>\d{4}-\d{2}-\d{2})_(?<hour>\d{2})-(?<minute>\d{2})/;
  const match = filename.match(re);
  if (match) {
    const { date, hour, minute } = match.groups;
    return `${date} ${hour}:${minute}`;
  } else {
    return filename;
  }
}

function debounce(callback, delay) {
  let timeout;
  return function(...args) {
    clearTimeout(timeout);
    timeout = setTimeout(() => callback(...args), delay);
  };
}

function render() {
  elGalleryGrid.textContent = "";

  const query = elSearchText.value.trim().toLowerCase();
  const filtered = MEDIA
    .map((src, idx) => ({ ...src, idx }))
    .filter(o => !query || o.src.toLowerCase().includes(query));

  elSearchCount.textContent = `${filtered.length.toLocaleString()} / ${MEDIA.length.toLocaleString()}`;

  for (let i = 0; i < filtered.length; i++) {
    const src = filtered[i].src;
    const kind = typeOf(src);

    const elTile = document.createElement("div");
    elTile.className = "tile";

    const elTileBox = document.createElement("a");
    elTileBox.className = "tile-box";
    elTileBox.href = src;

    if (kind == "image") {
      const elImg = document.createElement("img");
      elImg.loading = "lazy";
      elImg.src = src;

      elTileBox.appendChild(elImg);
    } else if (kind == "video") {
      const elBadge = document.createElement("span");
      elBadge.className = "thumb-type-badge";
      elBadge.textContent = "\u{1F3A5}\uFE0E";

      const elVideo = document.createElement("video");
      elVideo.preload = "metadata";
      elVideo.muted = true;
      elVideo.playsInline = true;

      const elSource = document.createElement("source");
      elSource.src = src;
      elSource.type = "video/mp4";

      elVideo.appendChild(elSource);

      elTileBox.appendChild(elBadge);
      elTileBox.appendChild(elVideo);
    }

    const elTileFooter = document.createElement("div");
    elTileFooter.className = "tile-footer";

    const elBacklinkBadge = document.createElement("a");
    elBacklinkBadge.className = "backlink-badge";
    elBacklinkBadge.textContent = "\u{1F5E8}\uFE0E";
    elBacklinkBadge.href = `${window.CHAT_FILE_URL}#${filtered[i].messageId}`;

    const elLabel = document.createElement("div");
    elLabel.className = "tile-label";

    elLabel.textContent = timestampFromPath(src);

    elTileFooter.appendChild(elBacklinkBadge);
    elTileFooter.appendChild(elLabel);

    elTile.appendChild(elTileBox);
    elTile.appendChild(elTileFooter);

    elGalleryGrid.appendChild(elTile);
  }
}

elSearchText.addEventListener("input", debounce(() => render(), 250));
render();
