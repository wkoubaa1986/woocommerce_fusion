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

		// Retouche manuelle : repasser à l'IA les images choisies, traitées ou non, avec une consigne
		frm.add_custom_button(__("Retoucher des images avec l’IA…"), function () {
			frm.trigger("sync_item_images");
		}, __('Actions'));

		// Ordre des images, image principale, images gardées hors du site
		frm.add_custom_button(__("Images : ordre et site…"), function () {
			wcf_synchro.fenetre_rangement(frm);
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
		wcf_synchro.lancer(frm);
	},

	sync_item_images: function(frm) {
		wcf_synchro.fenetre_retouche(frm);
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
			const quoi = e.titre || __("Synchronisation avec le site");
			const titre = e.etat === "attente"
				? "🔄 " + quoi + " : " + __("en file d’attente")
				: "🔄 " + quoi + " — " + __("étape") + " " + e.rang + " " + __("sur") + " " + e.total
					+ " : " + e.libelle + detail;
			return { genre: "cours", titre, lignes: avert.map((a) => "⚠️ " + a),
				barre: e.total ? Math.round(100 * Math.max(0, (e.rang || 0) - 0.5) / e.total) : 5,
				depuis: e.debut ? this.duree(sec(e.debut, maintenant)) : "" };
		}
		if (e.etat === "a_valider") {
			const n = (e.propositions || []).length;
			return { genre: "attention", a_valider: true,
				titre: "🖼️ " + n + " " + __("retouche(s) prête(s) : à vérifier avant de les garder"),
				lignes: [__("Rien n’a encore été remplacé, rien n’est parti sur le site.")].concat(avert.map((a) => "⚠️ " + a)) };
		}
		if (!e.fin || sec(e.fin, maintenant) > this.GARDER_H * 3600) return null;
		const heure = String(e.fin).substring(11, 16);
		if (e.etat === "echec") {
			return { genre: "echec", titre: "❌ " + (e.rate || __("La synchronisation avec le site a échoué")) + " (" + heure + ")"
				+ (e.libelle ? " — " + __("étape") + " « " + e.libelle + " »" : ""),
				lignes: [e.message || __("Cause inconnue : voir le journal des erreurs.")].concat(avert.map((a) => "⚠️ " + a)) };
		}
		return { genre: avert.length ? "attention" : "ok",
			titre: (avert.length ? "⚠️ " + (e.fait || __("Synchronisé avec le site")) + " " + __("à") + " " + heure + " — " + avert.length + " " + __("point(s) à voir")
				: "✅ " + (e.fait || __("Synchronisé avec le site")) + " " + __("à") + " " + heure) + (e.debut ? " (" + this.duree(sec(e.debut, e.fin)) + ")" : ""),
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
				${vue.a_valider ? `<button class="btn btn-xs btn-primary" data-wcf="verifier">${__("Vérifier les retouches")}</button>`
					: fini ? `<button class="btn btn-xs btn-default" data-wcf="relancer">${__("Relancer")}</button>
					<button class="btn btn-xs btn-default" data-wcf="masquer">${__("Masquer")}</button>` : ""}
			</div>
			${vue.genre === "cours" ? `<div style="height:6px;background:#dbeafe;border-radius:999px;margin-top:8px;overflow:hidden">
				<div style="height:100%;width:${vue.barre}%;background:#2563eb;border-radius:999px;transition:width .6s"></div></div>
				<div style="margin-top:6px;opacity:.85">${__("Vous pouvez continuer à travailler : la synchronisation se poursuit en arrière-plan.")}</div>` : ""}
			${(vue.lignes || []).map((l) => `<div style="margin-top:4px">${esc(l)}</div>`).join("")}`).show();
		// une retouche ne se rejoue pas telle quelle (les images ont été remplacées) : on rouvre la fenêtre de choix
		$b.find('[data-wcf="relancer"]').on("click", () => (e.genre === "synchro" || !e.genre ? this.lancer(frm) : this.fenetre_retouche(frm)));
		$b.find('[data-wcf="verifier"]').on("click", () => this.fenetre_validation(frm, e));
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
		} else if (e && e.etat === "a_valider" && this.en_cours(avant) && avant.item === e.item) {
			this.fenetre_validation(frm, e);
		} else if (e && this.en_cours(avant) && avant.item === e.item) {
			// vient de se terminer sous nos yeux : on le dit, et on recharge la fiche (nouveaux textes, nouvelles images)
			frappe.show_alert({ message: e.etat === "echec" ? (e.rate || __("La synchronisation avec le site a échoué"))
				: (e.fait || __("Synchronisé avec le site")), indicator: e.etat === "echec" ? "red" : "green" }, 7);
			if (!frm.is_dirty()) frm.reload_doc();
		}
	},

	/** Une fois la tâche mise en file : le bandeau prend le relais. */
	parti(frm, e) {
		e = e || {};
		frm._wcf_masque = null;
		if (e.deja) frappe.show_alert({ message: __("Un traitement est déjà en cours pour cet article."), indicator: "orange" }, 5);
		this.recevoir(frm, e);
	},

	/** La tâche de fond lit la fiche ENREGISTRÉE : une case cochée mais pas encore enregistrée serait ignorée. */
	enregistrer_puis(frm, suite) {
		if (frm.is_dirty()) frm.save().then(suite);
		else suite();
	},

	lancer(frm) {
		this.enregistrer_puis(frm, () => frappe.call({ method: this.METHODE + "lancer", args: { item_code: frm.doc.name } })
			.then((r) => this.parti(frm, r.message)));
	},

	/**
	 * « Images : ordre et site » (23/11/2026 : « l'ordre des images, même l'image principale, est-ce que je peux le
	 * changer dans l'article et par conséquent sur le site » ; « sélectionner des images qui ne seront pas poussées sur
	 * le site »). Les flèches déplacent une image ; la première est l'image principale ; une case par image l'écarte
	 * du site.
	 */
	fenetre_rangement(frm) {
		const esc = frappe.utils.escape_html;
		frappe.call({ method: this.METHODE + "images", args: { item_code: frm.doc.name } }).then((r) => {
			const images = r.message || [];
			if (!images.length) {
				frappe.msgprint(__("Cet article n’a aucune image jointe."));
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __("Images : ordre et site"),
				size: "large",
				fields: [
					{ fieldtype: "HTML", fieldname: "liste" },
					{ fieldtype: "Check", fieldname: "synchroniser", label: __("Puis envoyer l’article sur le site"), default: 1 },
				],
				primary_action_label: __("Enregistrer"),
				primary_action: (v) => {
					const $t = d.$wrapper.find("[data-wcf-rang]");
					const ordre = $t.map((i, el) => $(el).attr("data-wcf-rang")).get();
					const hors = $t.filter((i, el) => $(el).find("[data-wcf-hors]").is(":checked")).map((i, el) => $(el).attr("data-wcf-rang")).get();
					d.hide();
					this.enregistrer_puis(frm, () => frappe.call({
						method: this.METHODE + "enregistrer_images",
						args: { item_code: frm.doc.name, ordre: ordre, hors_site: hors, synchroniser: v.synchroniser ? 1 : 0 },
					}).then((r2) => {
						const e = r2.message || {};
						if (e.enregistre) {
							frappe.show_alert({ message: __("Ordre des images enregistré."), indicator: "green" }, 5);
							frm.reload_doc();
						} else this.parti(frm, e);
					}));
				},
			});
			const $liste = d.fields_dict.liste.$wrapper;
			$liste.html(`
				<div style="margin-bottom:8px;color:var(--text-muted)">${__("Déplacez les images avec les flèches. La première est l’image principale de l’article et du produit sur le site. Cochez « Pas sur le site » pour garder une image dans ERPNext sans l’envoyer.")}</div>
				<div data-wcf-grille style="display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px">
				${images.map((im) => `
					<div data-wcf-rang="${esc(im.name)}" style="border:1px solid var(--border-color);border-radius:10px;padding:8px">
						<div data-wcf-place style="font-size:11.5px;font-weight:700;margin-bottom:4px"></div>
						<div style="height:140px;display:flex;align-items:center;justify-content:center;background:#fff;border-radius:6px;overflow:hidden">
							<img src="${esc(im.file_url)}" style="max-width:100%;max-height:140px" loading="lazy">
						</div>
						<div style="display:flex;gap:6px;margin-top:6px">
							<button class="btn btn-xs btn-default" data-wcf-vers="-1" title="${__("Avancer")}" style="flex:1">◀</button>
							<button class="btn btn-xs btn-default" data-wcf-vers="1" title="${__("Reculer")}" style="flex:1">▶</button>
							<button class="btn btn-xs btn-default" data-wcf-vers="0" title="${__("Mettre en image principale")}" style="flex:1">⭐</button>
						</div>
						<label style="display:flex;align-items:center;gap:6px;margin:6px 0 0;font-size:12px;cursor:pointer">
							<input type="checkbox" data-wcf-hors ${im.hors_site ? "checked" : ""} style="margin:0"> ${__("Pas sur le site")}
						</label>
					</div>`).join("")}
				</div>`);
			const numeroter = () => $liste.find("[data-wcf-rang]").each((i, el) => {
				const hors = $(el).find("[data-wcf-hors]").is(":checked");
				$(el).css("opacity", hors ? 0.55 : 1);
				$(el).find("[data-wcf-place]").text(i === 0 ? "⭐ " + __("1 · image principale") : String(i + 1))
					.css("color", i === 0 ? "#166534" : "var(--text-muted)");
			});
			$liste.on("click", "[data-wcf-vers]", (ev) => {
				const $c = $(ev.currentTarget).closest("[data-wcf-rang]");
				const sens = parseInt($(ev.currentTarget).attr("data-wcf-vers"), 10);
				if (sens === 0) $c.parent().prepend($c);
				else if (sens < 0 && $c.prev().length) $c.insertBefore($c.prev());
				else if (sens > 0 && $c.next().length) $c.insertAfter($c.next());
				numeroter();
			});
			$liste.on("change", "[data-wcf-hors]", numeroter);
			numeroter();
			d.show();
		});
	},

	/**
	 * Contrôle avant (23/11/2026, après une retouche fausse partie directement sur le site) : chaque retouche est
	 * montrée à côté de l'image actuelle ; rien n'est remplacé tant que l'utilisateur n'a pas coché « garder ».
	 */
	fenetre_validation(frm, e) {
		const esc = frappe.utils.escape_html;
		const props = e.propositions || [];
		if (!props.length) return;
		if (this._validation && this._validation.display) this._validation.hide();
		const d = new frappe.ui.Dialog({
			title: __("Vérifier les retouches avant de les garder"),
			size: "extra-large",
			fields: [{ fieldtype: "HTML", fieldname: "paires" }],
			primary_action_label: __("Valider mon choix"),
			primary_action: () => {
				const gardees = d.$wrapper.find("[data-wcf-garder]:checked").map((i, el) => $(el).attr("data-wcf-garder")).get();
				d.hide();
				frappe.call({ method: this.METHODE + "valider_retouches", args: { item_code: frm.doc.name, gardees: gardees } })
					.then((r) => this.parti(frm, r.message));
			},
			secondary_action_label: __("Plus tard"),
			secondary_action: () => d.hide(),
		});
		const image = (url, legende) => `
			<div style="flex:1 1 0;min-width:0">
				<div style="font-size:11.5px;font-weight:700;color:var(--text-muted);margin-bottom:4px">${legende}</div>
				<a href="${esc(url)}" target="_blank" title="${__("Ouvrir en grand")}">
					<img src="${esc(url)}" style="width:100%;max-height:360px;object-fit:contain;border:1px solid var(--border-color);border-radius:8px;background:#fff" loading="lazy"></a>
			</div>`;
		d.fields_dict.paires.$wrapper.html(`
			<div style="margin-bottom:10px">${__("Comparez chaque image. Cochez « Garder la retouche » seulement si elle est juste : mêmes cotes, même produit, mêmes couleurs. Une retouche non cochée est jetée, l’image actuelle reste.")}</div>
			${props.map((p, i) => `
				<div style="border:1px solid var(--border-color);border-radius:10px;padding:10px;margin-bottom:12px">
					<div style="margin-bottom:8px;font-size:12.5px"><b>${__("Votre consigne")} :</b> ${p.consigne ? esc(p.consigne) : `<span style="color:var(--text-muted)">${__("aucune (retouche habituelle)")}</span>`}</div>
					<div style="display:flex;gap:12px;align-items:flex-start">
						${image(p.ancien_url, __("Image actuelle"))}
						${image(p.proposition_url, __("Retouche proposée"))}
					</div>
					<label style="display:flex;align-items:center;gap:8px;margin:10px 0 0;font-weight:700;cursor:pointer">
						<input type="checkbox" data-wcf-garder="${esc(p.ancien)}" style="margin:0"> ${__("Garder la retouche")} (${i + 1} / ${props.length})
					</label>
				</div>`).join("")}
			<div style="color:var(--text-muted)">${e.synchroniser ? __("Après validation, l’article sera envoyé sur le site avec les retouches gardées.") : __("L’article ne sera pas envoyé sur le site.")}</div>`);
		this._validation = d;
		d.show();
	},

	/**
	 * Retouche manuelle (demande du 23/11/2026 : « lorsque c'est traité par l'IA on ne la retraite plus, mais si elle
	 * est traitée on peut la retraiter, une image, en ajoutant une consigne »). Le passage automatique ne touche
	 * jamais une image déjà traitée ; ici l'utilisateur choisit les images, écrit ce qu'il veut, et les relance.
	 */
	fenetre_retouche(frm) {
		const esc = frappe.utils.escape_html;
		frappe.call({ method: this.METHODE + "images", args: { item_code: frm.doc.name } }).then((r) => {
			const images = r.message || [];
			if (!images.length) {
				frappe.msgprint(__("Cet article n’a aucune image jointe."));
				return;
			}
			const d = new frappe.ui.Dialog({
				title: __("Retoucher des images avec l’IA"),
				size: "large",
				fields: [
					{ fieldtype: "HTML", fieldname: "choix" },
					{ fieldtype: "Check", fieldname: "synchroniser", label: __("Envoyer l’article sur le site une fois mon choix validé"), default: 1 },
				],
				primary_action_label: __("Retoucher, puis me montrer le résultat"),
				secondary_action_label: __("↩ Revenir à l’image d’avant"),
				secondary_action: () => {
					const cochees = d.$wrapper.find("[data-wcf-image]:checked");
					const fichiers = cochees.filter("[data-wcf-avant='1']").map((i, el) => $(el).attr("data-wcf-image")).get();
					if (!fichiers.length) {
						frappe.show_alert({ message: __("Cochez une image qui a une version d’avant (marquée ↩)."), indicator: "orange" }, 5);
						return;
					}
					const envoyer = d.get_value("synchroniser") ? 1 : 0;
					d.hide();
					this.enregistrer_puis(frm, () => frappe.call({
						method: this.METHODE + "restaurer", args: { item_code: frm.doc.name, fichiers: fichiers, synchroniser: envoyer },
					}).then((r2) => this.parti(frm, r2.message)));
				},
				primary_action: (v) => {
					const fichiers = d.$wrapper.find("[data-wcf-image]:checked").map((i, el) => $(el).attr("data-wcf-image")).get();
					if (!fichiers.length) {
						frappe.show_alert({ message: __("Cochez au moins une image."), indicator: "orange" }, 4);
						return;
					}
					d.hide();
					const consignes = {};
					fichiers.forEach((f) => {
						const texte = (d.$wrapper.find(`[data-wcf-consigne="${f}"]`).val() || "").trim();
						if (texte) consignes[f] = texte;
					});
					this.enregistrer_puis(frm, () => frappe.call({
						method: this.METHODE + "lancer_retouche",
						args: { item_code: frm.doc.name, fichiers: fichiers, consignes: consignes, synchroniser: v.synchroniser ? 1 : 0 },
					}).then((r2) => this.parti(frm, r2.message)));
				},
			});
			d.fields_dict.choix.$wrapper.html(`
				<div style="margin-bottom:8px;color:var(--text-muted)">${__("Cochez les images à repasser à l’IA et écrivez, si vous voulez, une consigne POUR CHAQUE image (exemples : « enlève le reflet sur le capot », « garde le fond d’origine », « écris les cotes en rouge »). Sans consigne, la retouche habituelle est refaite. Rien n’est remplacé tout de suite : vous verrez l’avant / après et vous choisirez.")}</div>
				<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:10px">
				${images.map((im) => `
					<div style="border:1px solid var(--border-color);border-radius:10px;padding:8px">
						<label style="cursor:pointer;display:block;margin:0">
						<div style="height:150px;display:flex;align-items:center;justify-content:center;background:#fff;border-radius:6px;overflow:hidden">
							<img src="${esc(im.file_url)}" style="max-width:100%;max-height:150px" loading="lazy">
						</div>
						<div style="display:flex;align-items:center;gap:6px;margin-top:6px">
							<input type="checkbox" data-wcf-image="${esc(im.name)}" data-wcf-avant="${im.avant_url ? 1 : 0}" style="margin:0">
							<span style="font-size:11.5px;font-weight:600;color:${im.traitee ? "#166534" : "#9a3412"}">${im.traitee ? "✓ " + __("traitée par l’IA") : __("pas encore traitée")}</span>
						</div>
						${im.principale ? `<div style="font-size:11px;color:var(--text-muted)">${__("image principale")}</div>` : ""}
						${im.hors_site ? `<div style="font-size:11px;color:#9a3412">${__("pas sur le site")}</div>` : ""}
						</label>
						${im.avant_url ? `<div style="font-size:11px"><a href="${esc(im.avant_url)}" target="_blank" title="${__("Voir l’image d’avant la retouche")}">↩ ${__("image d’avant disponible")}</a></div>` : ""}
						<textarea class="form-control" data-wcf-consigne="${esc(im.name)}" rows="2" maxlength="600"
							placeholder="${__("Consigne pour cette image (facultatif)")}" style="margin-top:6px;font-size:12px;min-height:52px"></textarea>
					</div>`).join("")}
				</div>`);
			d.show();
		});
	},
};
window.wcf_synchro = wcf_synchro;
