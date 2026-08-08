"use strict";

(() => {
  const key = "arcade-proof.operations.v1";

  function storage() {
    for (const name of ["localStorage", "sessionStorage"]) {
      try {
        const candidate = window[name];
        candidate.getItem(key);
        return candidate;
      } catch (_error) {
        // Try the next browser-owned storage boundary.
      }
    }
    return null;
  }

  function read() {
    try {
      return JSON.parse(storage()?.getItem(key) || "{}") || {};
    } catch (_error) {
      return {};
    }
  }

  function write(entries) {
    try {
      const backend = storage();
      if (!backend) return false;
      backend.setItem(key, JSON.stringify(entries));
      return true;
    } catch (_error) {
      return false;
    }
  }

  function entryKey(playerId, gameKey) {
    return `${playerId}:${gameKey}`;
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.journal = {
    get(playerId, gameKey) {
      return read()[entryKey(playerId, gameKey)] || null;
    },
    save(playerId, gameKey, operation) {
      const entries = read();
      entries[entryKey(playerId, gameKey)] = {
        requestId: null,
        sessionId: null,
        commitmentId: null,
        clientSeed: null,
        outcome: null,
        pendingAction: null,
        ...operation,
        updatedAt: new Date().toISOString(),
      };
      return write(entries);
    },
    discard(playerId, gameKey) {
      const entries = read();
      delete entries[entryKey(playerId, gameKey)];
      return write(entries);
    },
    clear() {
      return write({});
    },
  };
})();
