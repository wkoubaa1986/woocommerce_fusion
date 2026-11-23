frappe.ui.form.on('Item', {
	refresh: function(frm) {
		// Add a custom button to sync Item Stock with WooCommerce
		frm.add_custom_button(__("Sync this Item's Stock Levels to WooCommerce"), function () {
			frm.trigger("sync_item_stock");
		}, __('Actions'));

		// Add a custom button to sync Item Price with WooCommerce
		frm.add_custom_button(__("Sync this Item's Price to WooCommerce"), function () {
			frm.trigger("sync_item_price");
		}, __('Actions'));

		// Add a custom button to sync Item with WooCommerce
		frm.add_custom_button(__("Sync this Item with WooCommerce"), function () {
			frm.trigger("sync_item");
		}, __('Actions'));

		// Refaire la retouche de toutes les images (après une retouche ratée ou un changement de consigne)
		frm.add_custom_button(__("Refaire la retouche des images et synchroniser"), function () {
			frm.trigger("sync_item_images");
		}, __('Actions'));

		// Où en est la synchronisation de cet article avec le site (en cours, terminée, échouée)
		wcf_synchro.suivre(frm);
	},

	sync_item_stock: function(frm) {
		// Sync this Item
		frappe.dom.freeze(__("Sync Item Stock with WooCommerce..."));
		frappe.call({
			method: "woocommerce_fusion.tasks.stock_update.update_stock_levels_on_woocommerce_site",
			args: {
				item_code: frm.doc.name
			},
			callback: function(r) {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message:__('Synchronised stock level to WooCommerce for enabled servers'),
					indicator:'green'
				}, 5);
				frm.reload_doc();
			},
			error: (r) => {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('There was an error processing the request. See Error Log.'),
					indicator: 'red'
				}, 5);
			}
		});
	},

	sync_item_price: function(frm) {
		// Sync this Item's Price
		frappe.dom.freeze(__("Sync Item Price with WooCommerce..."));
		frappe.call({
			method: "woocommerce_fusion.tasks.sync_item_prices.run_item_price_sync",
			args: {
				item_code: frm.doc.name
			},
			callback: function(r) {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message:__('Synchronised item price to WooCommerce'),
					indicator:'green'
				}, 5);
				frm.reload_doc();
			},
			error: (r) => {
				frappe.dom.unfreeze();
				frappe.show_alert({
					message: __('There was an error processing the request. See Error Log.'),
					indicator: 'red'
				}, 5);
			}
		});
	},

	sync_item: function(frm) {
		// Synchronisation complète : en tâche de fond, avec son avancement sur la fiche (wcf_synchro plus bas).
		wcf_synchro.lancer(frm, 0);
	},

	sync_item_images: function(frm) {
		frappe.confirm(
			__("Toutes les images de cet article repassent à la retouche, même celles déjà traitées, puis l’article est synchronisé avec le site. Continuer ?"),
			() => wcf_synchro.lancer(frm, 1)
		);
	},
})

frappe.ui.form.on('Item WooCommerce Server', {
	view_product: function(frm, cdt, cdn) {
		let current_row_doc = locals[cdt][cdn];
		console.log(current_row_doc);
		frappe.set_route("Form", "WooCommerce Product", `${current_row_doc.woocommerce_server}~${current_row_doc.woocommerce_id}` );
	}
})

/**
 * Synchronisation d'un article avec le site, en tâche de fond (demande du 23/11/2026 : « passer le job en background et
 * avoir un indicateur sur la fiche produit : qu'elle est en train de synchroniser, et à quelle étape »).
 *
 * Le serveur (woocommerce_fusion.tasks.suivi_synchro) tient l'état : en file d'attente, en cours (étape n sur N, avec
 * le détail « image 2 sur 4 »), terminée ou en échec, plus ce qui a été fait et ce qui n'a pas pu l'être. La fiche
 * l'affiche dans un bandeau, le reçoit en direct, et le relit toutes les 4 secondes tant que ça tourne.
 */
const wcf_synchro = {
	METHODE: "woocommerce_fusion.tasks.suivi_synchro.",
	RELIRE_MS: 4000,
	GARDER_H: 12, // un résultat reste affiché douze heures

	en_cours(e) { return !!e && (e.etat === "attente" || e.etat === "en_cours"); },

	/** « 1 min 20 », « 45 s ». Pure. */
	duree(secondes) {
		secondes = Math.max(0, Math.round(secondes));
		return secondes < 60 ? secondes + " s" : Math.floor(secondes / 60) + " min " + String(secondes % 60).padStart(2, "0");
	},

	/** Ce que le bandeau doit dire pour un état donné : { genre, titre, lignes, barre }. Pure (hors traduction). */
	lire(e, maintenant) {
		if (!e) return null;
		const sec = (a, b) => (new Date(String(b).replace(" ", "T")) - new Date(String(a).replace(" ", "T"))) / 1000;
		const avert = e.avertissements || [];
		const resume = e.resume || [];
		if (this.en_cours(e)) {
			const detail = e.detail ? " (" + e.detail + ")" : "";
			const titre = e.etat === "attente"
				? "🔄 " + __("Synchronisation avec le site : en file d’attente")
				: "🔄 " + __("Synchronisation avec le site") + " — " + __("étape") + " " + e.rang + " " + __("sur") + " " + e.total
					+ " : " + e.libelle + detail;
			return { genre: "cours", titre, lignes: avert.map((a) => "⚠️ " + a),
				barre: e.total ? Math.round(100 * Math.max(0, (e.rang || 0) - 0.5) / e.total) : 5,
				depuis: e.debut ? this.duree(sec(e.debut, maintenant)) : "" };
		}
		if (!e.fin || sec(e.fin, maintenant) > this.GARDER_H * 3600) return null;
		const heure = String(e.fin).substring(11, 16);
		if (e.etat === "echec") {
			return { genre: "echec", titre: "❌ " + __("La synchronisation avec le site a échoué") + " (" + heure + ")"
				+ (e.libelle ? " — " + __("étape") + " « " + e.libelle + " »" : ""),
				lignes: [e.message || __("Cause inconnue : voir le journal des erreurs.")].concat(avert.map((a) => "⚠️ " + a)) };
		}
		return { genre: avert.length ? "attention" : "ok",
			titre: (avert.length ? "⚠️ " + __("Synchronisé avec le site à") + " " + heure + " — " + avert.length + " " + __("point(s) à voir")
				: "✅ " + __("Synchronisé avec le site à") + " " + heure) + (e.debut ? " (" + this.duree(sec(e.debut, e.fin)) + ")" : ""),
			lignes: avert.map((a) => "⚠️ " + a).concat(resume.length ? ["✓ " + resume.join(" · ")] : []) };
	},

	/** L'heure du SERVEUR à cet instant : celle reçue avec l'état, avancée du temps passé depuis. La fiche ne dépend
	 *  ainsi ni de l'horloge ni du fuseau du navigateur (un compte réglé sur un autre fuseau affichait « 270 min »). */
	maintenant(e) {
		if (!e || !e.maintenant) return frappe.datetime.now_datetime();
		const base = new Date(String(e.maintenant).replace(" ", "T")).getTime() + (Date.now() - (e._recu || Date.now()));
		const d = new Date(base), p = (n) => String(n).padStart(2, "0");
		return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
	},

	bandeau(frm) {
		let $b = frm.$wrapper.find(".wcf-synchro");
		if (!$b.length) {
			$b = $('<div class="wcf-synchro" style="display:none;margin:0 0 12px;padding:10px 14px;border-radius:10px;border:1px solid;font-size:13px"></div>');
			frm.$wrapper.find(".form-layout").first().prepend($b);
		}
		return $b;
	},

	afficher(frm, e) {
		const $b = this.bandeau(frm);
		const vue = this.lire(e, this.maintenant(e));
		if (!vue || (e && frm._wcf_masque === e.fin && !this.en_cours(e))) { $b.hide().empty(); return; }
		const couleurs = { cours: ["#eff6ff", "#93c5fd", "#1e3a8a"], ok: ["#f0fdf4", "#86efac", "#166534"],
			attention: ["#fff7ed", "#fdba74", "#9a3412"], echec: ["#fef2f2", "#fca5a5", "#991b1b"] }[vue.genre];
		const esc = frappe.utils.escape_html;
		const fini = vue.genre !== "cours";
		$b.css({ background: couleurs[0], "border-color": couleurs[1], color: couleurs[2] }).html(`
			<div style="display:flex;gap:10px;align-items:flex-start;flex-wrap:wrap">
				<div style="flex:1 1 260px;font-weight:700">${esc(vue.titre)}${vue.depuis ? ` <span style="font-weight:400;opacity:.8">· ${esc(vue.depuis)}</span>` : ""}</div>
				${fini ? `<button class="btn btn-xs btn-default" data-wcf="relancer">${__("Relancer")}</button>
					<button class="btn btn-xs btn-default" data-wcf="masquer">${__("Masquer")}</button>` : ""}
			</div>
			${vue.genre === "cours" ? `<div style="height:6px;background:#dbeafe;border-radius:999px;margin-top:8px;overflow:hidden">
				<div style="height:100%;width:${vue.barre}%;background:#2563eb;border-radius:999px;transition:width .6s"></div></div>
				<div style="margin-top:6px;opacity:.85">${__("Vous pouvez continuer à travailler : la synchronisation se poursuit en arrière-plan.")}</div>` : ""}
			${(vue.lignes || []).map((l) => `<div style="margin-top:4px">${esc(l)}</div>`).join("")}`).show();
		$b.find('[data-wcf="relancer"]').on("click", () => this.lancer(frm, 0));
		$b.find('[data-wcf="masquer"]').on("click", () => { frm._wcf_masque = e.fin; $b.hide(); });
	},

	/** À chaque ouverture ou rafraîchissement de la fiche : lire l'état, et continuer à le relire tant que ça tourne. */
	suivre(frm) {
		if (frm.is_new()) return;
		if (!this._direct) {
			this._direct = true; // une seule écoute pour toute la session
			frappe.realtime.on("wcf_synchro", (e) => {
				const f = window.cur_frm;
				if (f && f.doctype === "Item" && e && f.doc.name === e.item) this.recevoir(f, e);
			});
		}
		frappe.call({ method: this.METHODE + "etat", args: { item_code: frm.doc.name } })
			.then((r) => this.recevoir(frm, r.message));
	},

	recevoir(frm, e) {
		if (e) e._recu = Date.now();
		const avant = frm._wcf_etat;
		frm._wcf_etat = e || null;
		this.afficher(frm, e);
		clearTimeout(frm._wcf_minuterie);
		if (this.en_cours(e)) {
			frm._wcf_minuterie = setTimeout(() => {
				// la fiche a changé d'article ou a été quittée : on arrête de relire
				if (window.cur_frm !== frm || frm.doc.name !== e.item || !frm.$wrapper.is(":visible")) return;
				frappe.call({ method: this.METHODE + "etat", args: { item_code: e.item } }).then((r) => this.recevoir(frm, r.message));
			}, this.RELIRE_MS);
		} else if (e && this.en_cours(avant) && avant.item === e.item) {
			// vient de se terminer sous nos yeux : on le dit, et on recharge la fiche (nouveaux textes, nouvelles images)
			frappe.show_alert({ message: e.etat === "echec" ? __("La synchronisation avec le site a échoué")
				: __("Synchronisation avec le site terminée"), indicator: e.etat === "echec" ? "red" : "green" }, 7);
			if (!frm.is_dirty()) frm.reload_doc();
		}
	},

	lancer(frm, refaire_images) {
		const partir = () => frappe.call({
			method: this.METHODE + "lancer", args: { item_code: frm.doc.name, refaire_images: refaire_images ? 1 : 0 },
		}).then((r) => {
			const e = r.message || {};
			frm._wcf_masque = null;
			if (e.deja) frappe.show_alert({ message: __("Une synchronisation est déjà en cours pour cet article."), indicator: "orange" }, 5);
			this.recevoir(frm, e);
		});
		// La tâche de fond lit la fiche ENREGISTRÉE : une case cochée mais pas encore enregistrée serait ignorée.
		if (frm.is_dirty()) frm.save().then(partir);
		else partir();
	},
};
window.wcf_synchro = wcf_synchro;
