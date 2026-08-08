"use strict";

(() => {
  const storageKey = "arcade-proof.players.v1";
  let memoryValue = null;
  let validationPromise = null;

  function availableStorage(name) {
    try {
      const storage = window[name];
      const key = "arcade-proof.storage-check";
      storage.setItem(key, key);
      storage.removeItem(key);
      return storage;
    } catch (_error) {
      return null;
    }
  }

  const localBackend = availableStorage("localStorage");
  const backend = localBackend || availableStorage("sessionStorage");
  const persistent = Boolean(localBackend);

  function read() {
    try {
      const raw = backend ? backend.getItem(storageKey) : memoryValue;
      if (!raw) return { currentId: null, players: [] };
      const parsed = JSON.parse(raw);
      if (!Array.isArray(parsed.players)) throw new Error("Invalid player store");
      return parsed;
    } catch (_error) {
      return { currentId: null, players: [] };
    }
  }

  function write(value) {
    const raw = JSON.stringify(value);
    if (backend) backend.setItem(storageKey, raw);
    else memoryValue = raw;
  }

  function emit(player) {
    window.dispatchEvent(new CustomEvent("arcade-proof:player", { detail: player }));
  }

  const playerStore = {
    persistent,
    snapshot: read,
    current() {
      const state = read();
      return state.players.find((player) => player.id === state.currentId) || null;
    },
    save(player) {
      const state = read();
      const players = state.players.filter((candidate) => candidate.id !== player.id);
      players.unshift(player);
      write({ currentId: player.id, players: players.slice(0, 8) });
      emit(player);
      return player;
    },
    select(playerId) {
      const state = read();
      const player = state.players.find((candidate) => candidate.id === playerId) || null;
      if (!player) return null;
      write({ ...state, currentId: playerId });
      emit(player);
      return player;
    },
    remove(playerId) {
      const state = read();
      const players = state.players.filter((player) => player.id !== playerId);
      const currentId = state.currentId === playerId ? players[0]?.id || null : state.currentId;
      write({ currentId, players });
      emit(players.find((player) => player.id === currentId) || null);
    },
    clear() {
      write({ currentId: null, players: [] });
      emit(null);
    },
    async validateCurrent() {
      if (validationPromise) return validationPromise;
      const player = this.current();
      if (!player) return null;
      validationPromise = (async () => {
        try {
          const validated = await window.ArcadeProof.api.getPlayer(player.id);
          return this.save(validated);
        } catch (error) {
          if (error.code === "not_found" || error.code === "forbidden") this.remove(player.id);
          throw error;
        }
      })();
      try {
        return await validationPromise;
      } finally {
        validationPromise = null;
      }
    },
  };

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.playerStore = playerStore;
})();
