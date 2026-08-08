"use strict";

(() => {
  class ServerClock {
    constructor(serverTime) {
      this.offsetMs = new Date(serverTime).getTime() - Date.now();
    }

    now() {
      return Date.now() + this.offsetMs;
    }

    remaining(target) {
      return Math.max(0, new Date(target).getTime() - this.now());
    }

    label(target) {
      const seconds = Math.ceil(this.remaining(target) / 1000);
      if (seconds < 60) return `${seconds}s`;
      const minutes = Math.ceil(seconds / 60);
      if (minutes < 60) return `${minutes}m`;
      return `${Math.ceil(minutes / 60)}h`;
    }
  }

  window.ArcadeProof = window.ArcadeProof || {};
  window.ArcadeProof.ServerClock = ServerClock;
})();
