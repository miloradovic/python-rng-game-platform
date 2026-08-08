"use strict";

document.addEventListener("alpine:init", () => {
  window.Alpine.data("shell", () => ({
    navOpen: false,
    playerLabel: window.ArcadeProof.playerStore.current()?.display_name || "Choose player",

    get navClass() {
      return this.navOpen ? "is-open" : "";
    },

    markReady() {
      document.documentElement.dataset.alpineReady = "true";
      window.addEventListener("arcade-proof:player", (event) => {
        this.playerLabel = event.detail?.display_name || "Choose player";
      });
    },

    toggleNavigation() {
      this.navOpen = !this.navOpen;
    },

    openIdentity() {
      window.ArcadeProof.dialogTrigger = document.activeElement;
      const dialog = document.getElementById("identity-dialog");
      if (dialog && !dialog.open) dialog.showModal();
    },

    openPreferences() {
      window.ArcadeProof.dialogTrigger = document.activeElement;
      const dialog = document.getElementById("preferences-dialog");
      if (dialog && !dialog.open) dialog.showModal();
    },
  }));
  window.Alpine.data("identityManager", window.ArcadeProof.components.identityManager);
  window.Alpine.data("preferenceManager", window.ArcadeProof.components.preferenceManager);
  window.Alpine.data("networkStatus", window.ArcadeProof.components.networkStatus);
  window.Alpine.data("operationRecovery", window.ArcadeProof.components.operationRecovery);
  window.Alpine.data("toastRegion", window.ArcadeProof.components.toastRegion);
  window.Alpine.data("leaderboard", window.ArcadeProof.components.leaderboard);
  window.Alpine.data("dailySpin", window.ArcadeProof.games.dailySpin);
  window.Alpine.data("predictionCard", window.ArcadeProof.games.predictionCard);
  window.Alpine.data("skillCheck", window.ArcadeProof.games.skillCheck);
});
