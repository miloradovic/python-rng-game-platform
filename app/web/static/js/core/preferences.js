"use strict";

(() => {
  const storageKey = "arcade-proof.preferences.v1";
  const defaults = Object.freeze({ reduceMotion: false, sound: false });
  let memoryValue = null;
  let audioContext = null;

  function backend() {
    try {
      const storage = window.localStorage;
      const key = "arcade-proof.preference-check";
      storage.setItem(key, key);
      storage.removeItem(key);
      return storage;
    } catch (_error) {
      return null;
    }
  }

  const storage = backend();
  const media = window.matchMedia("(prefers-reduced-motion: reduce)");

  function read() {
    try {
      const raw = storage ? storage.getItem(storageKey) : memoryValue;
      if (!raw) return { ...defaults };
      const parsed = JSON.parse(raw);
      return {
        reduceMotion: parsed.reduceMotion === true,
        sound: parsed.sound === true,
      };
    } catch (_error) {
      return { ...defaults };
    }
  }

  function apply() {
    document.documentElement.dataset.reduceMotion = String(preferences.reducedMotion());
  }

  function write(value) {
    const next = {
      reduceMotion: value.reduceMotion === true,
      sound: value.sound === true,
    };
    const raw = JSON.stringify(next);
    if (storage) storage.setItem(storageKey, raw);
    else memoryValue = raw;
    apply();
    window.dispatchEvent(new CustomEvent("arcade-proof:preferences", { detail: next }));
    return next;
  }

  const preferences = {
    persistent: Boolean(storage),
    snapshot: read,
    reducedMotion() { return media.matches || read().reduceMotion; },
    save: write,
    playCue(kind = "info") {
      if (!read().sound) return;
      const Context = window.AudioContext || window.webkitAudioContext;
      if (!Context) return;
      try {
        audioContext = audioContext || new Context();
        const oscillator = audioContext.createOscillator();
        const gain = audioContext.createGain();
        oscillator.frequency.value = kind === "success" ? 660 : 440;
        gain.gain.setValueAtTime(0.025, audioContext.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.0001, audioContext.currentTime + 0.08);
        oscillator.connect(gain);
        gain.connect(audioContext.destination);
        oscillator.start();
        oscillator.stop(audioContext.currentTime + 0.08);
      } catch (_error) {
        // Text and visual feedback remain complete when audio is unavailable.
      }
    },
  };

  if (typeof media.addEventListener === "function") media.addEventListener("change", apply);
  else media.addListener(apply);

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.preferences = preferences;
  apply();
})();
