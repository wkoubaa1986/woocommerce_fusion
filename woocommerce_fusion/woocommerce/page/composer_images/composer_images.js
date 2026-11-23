/**
 * « Composer des images » — /app/composer-images (23/11/2026).
 *
 * Demande : « les articles qui n'ont pas d'image, je peux recomposer l'image en prenant plusieurs images des produits
 * ou des images que j'ajoute moi-même (cas des FRP) ; le paramétrer sur plusieurs articles et le lancer en // ».
 *
 * 1. La liste des articles (sans image par défaut), avec pour chacun les photos sources PROPOSÉES par le serveur
 *    d'après son nom et ses attributs : sa bouteille, sa vanne, son bac à sel. On retire, on ajoute (images d'un autre
 *    article ou fichiers déposés), pour une ligne ou pour toutes les lignes cochées.
 * 2. « Composer » : le lot part en arrière-plan, quatre images à la fois ; la page suit l'avancement.
 * 3. Chaque image est une proposition : on coche celles qu'on garde, les autres sont jetées.
 *
 * Tout le travail est côté serveur : woocommerce_fusion.tasks.composition_lot.
 */
frappe.pages["composer-images"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({ parent: wrapper, title: __("Composer des images"), single_column: true });
	wrapper.composer_images = new ComposerImages(page);
};

class ComposerImages {
	constructor(page) {
		this.page = page;
		this.M = "woocommerce_fusion.tasks.composition_lot.";
		this.MAX = 6;
		this.lignes = []; // [{ name, item_name, item_group, sources: [{name, file_url, de}], coche }]
		this.communes = []; // photos ajoutées à tous les articles cochés
		this.lot = null;
		this.esc = frappe.utils.escape_html;
		this.construire();
		this.charger();
	}

	construire() {
		const p = this.page;
		this.f_groupe = p.add_field({ fieldtype: "Link", options: "Item Group", fieldname: "groupe", label: __("Groupe d’articles"), change: () => this.charger() });
		this.f_texte = p.add_field({ fieldtype: "Data", fieldname: "texte", label: __("Code ou nom contient"), change: () => this.charger() });
		this.f_sans = p.add_field({ fieldtype: "Check", fieldname: "sans_image", label: __("Sans image seulement"), default: 1, change: () => this.charger() });
		p.set_primary_action(__("Composer les images cochées"), () => this.lancer());
		this.$corps = $(`
			<div style="padding:12px 0">
				<div data-zone="aide" style="color:var(--text-muted);margin-bottom:12px">${__("Chaque article reçoit des photos sources proposées d’après son nom et ses attributs (sa bouteille, sa vanne, son bac à sel). Corrigez-les, cochez les articles, puis lancez : les images sont composées en arrière-plan, quatre à la fois. Rien n’est ajouté aux articles avant votre vérification.")}</div>
				<div data-zone="communes" style="border:1px solid var(--border-color);border-radius:10px;padding:10px 12px;margin-bottom:12px"></div>
				<div data-zone="avancement" style="display:none;border:1px solid #93c5fd;background:#eff6ff;color:#1e3a8a;border-radius:10px;padding:10px 12px;margin-bottom:12px"></div>
				<div data-zone="resultats" style="margin-bottom:16px"></div>
				<div data-zone="liste"></div>
			</div>`).appendTo(p.body);
		this.$corps.on("change", "[data-coche]", (e) => {
			const l = this.ligne($(e.currentTarget).attr("data-coche"));
			if (l) l.coche = $(e.currentTarget).is(":checked");
			this.compter();
		});
		this.$corps.on("change", "[data-tout]", (e) => {
			const coche = $(e.currentTarget).is(":checked");
			this.lignes.forEach((l) => { l.coche = coche && l.sources.length + this.communes.length > 0; });
			this.dessiner_liste();
		});
		this.$corps.on("click", "[data-retirer]", (e) => {
			const $b = $(e.currentTarget), l = this.ligne($b.attr("data-article"));
			const liste = l ? l.sources : this.communes;
			liste.splice(liste.findIndex((s) => s.name === $b.attr("data-retirer")), 1);
			l ? this.dessiner_liste() : this.dessiner_communes();
		});
		this.$corps.on("click", "[data-ajouter]", (e) => {
			const l = this.ligne($(e.currentTarget).attr("data-ajouter"));
			this.choisir_sources((s) => {
				const liste = l ? l.sources : this.communes;
				if (liste.some((x) => x.name === s.name)) return;
				if (liste.length >= this.MAX) { frappe.show_alert({ message: __("Six photos sources au plus."), indicator: "orange" }, 4); return; }
				liste.push(s);
				l ? this.dessiner_liste() : this.dessiner_communes();
			});
		});
		this.$corps.on("click", "[data-valider]", () => this.valider());
		this.$corps.on("click", "[data-cocher-resultats]", () => this.$corps.find("[data-garder]").prop("checked", true));
		this.dessiner_communes();
	}

	ligne(code) { return this.lignes.find((l) => l.name === code); }

	vignettes(sources, article) {
		const esc = this.esc;
		return sources.map((s) => `
			<span style="position:relative;display:inline-block;margin:0 6px 6px 0" title="${esc(s.de || "")}">
				<img src="${esc(s.file_url)}" style="width:64px;height:64px;object-fit:contain;border:1px solid var(--border-color);border-radius:6px;background:#fff" loading="lazy">
				<a href="#" data-retirer="${esc(s.name)}" data-article="${esc(article || "")}" onclick="return false"
					style="position:absolute;top:-6px;right:-6px;background:#fff;border:1px solid var(--border-color);border-radius:50%;width:18px;height:18px;line-height:15px;text-align:center;font-weight:800;text-decoration:none;color:#b91c1c">×</a>
			</span>`).join("");
	}

	dessiner_communes() {
		this.$corps.find("[data-zone=communes]").html(`
			<div style="font-weight:700;margin-bottom:6px">${__("Photos ajoutées à tous les articles cochés")} <span style="font-weight:400;color:var(--text-muted)">(${__("facultatif")})</span></div>
			<div>${this.vignettes(this.communes, "")}<button class="btn btn-xs btn-default" data-ajouter="">+ ${__("Ajouter une photo")}</button></div>
			<div style="margin-top:10px;font-weight:700">${__("Consigne pour toutes les images")} <span style="font-weight:400;color:var(--text-muted)">(${__("facultatif")})</span></div>
			<textarea class="form-control" data-consigne rows="2" maxlength="600" style="margin-top:4px"
				placeholder="${__("Exemple : la vanne vissée sur le dessus de la bouteille, le bac à sel à droite, relié par un tube.")}">${this.esc(this.consigne || "")}</textarea>`);
		this.$corps.find("[data-consigne]").on("input", (e) => { this.consigne = e.currentTarget.value; });
	}

	charger() {
		const $l = this.$corps.find("[data-zone=liste]").html(`<div style="color:var(--text-muted)">${__("Chargement…")}</div>`);
		frappe.call({
			method: this.M + "articles",
			args: { groupe: this.f_groupe.get_value() || "", texte: this.f_texte.get_value() || "", sans_image: this.f_sans.get_value() ? 1 : 0 },
		}).then((r) => {
			this.lignes = (r.message || []).map((l) => Object.assign(l, { coche: false }));
			this.dessiner_liste();
		}).catch(() => $l.html(`<div style="color:#b91c1c">${__("La liste n’a pas pu être chargée.")}</div>`));
	}

	dessiner_liste() {
		const esc = this.esc;
		const $l = this.$corps.find("[data-zone=liste]");
		if (!this.lignes.length) {
			$l.html(`<div style="color:var(--text-muted)">${__("Aucun article ne correspond.")}</div>`);
			this.compter();
			return;
		}
		$l.html(`
			<div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
				<label style="margin:0;cursor:pointer;font-weight:700"><input type="checkbox" data-tout style="margin:0 6px 0 0"> ${__("Tout cocher")}</label>
				<span data-compte style="color:var(--text-muted)"></span>
			</div>
			${this.lignes.map((l) => `
				<div style="display:flex;gap:12px;align-items:flex-start;border:1px solid var(--border-color);border-radius:10px;padding:8px 10px;margin-bottom:8px">
					<input type="checkbox" data-coche="${esc(l.name)}" ${l.coche ? "checked" : ""} style="margin:6px 0 0">
					<div style="flex:0 0 260px;min-width:0">
						<a href="/app/item/${encodeURIComponent(l.name)}" target="_blank" style="font-weight:700">${esc(l.name)}</a>
						<div style="font-size:12px">${esc(l.item_name || "")}</div>
						<div style="font-size:11px;color:var(--text-muted)">${esc(l.item_group || "")}${l.has_variants ? " · " + __("modèle") : ""}</div>
					</div>
					<div style="flex:1 1 auto;min-width:0">
						${this.vignettes(l.sources, l.name)}
						<button class="btn btn-xs btn-default" data-ajouter="${esc(l.name)}">+ ${__("photo")}</button>
						${l.sources.length ? "" : `<div style="font-size:11.5px;color:#9a3412">${__("Aucune photo source proposée : ajoutez-en une.")}</div>`}
					</div>
				</div>`).join("")}`);
		this.compter();
	}

	compter() {
		const n = this.lignes.filter((l) => l.coche).length;
		this.$corps.find("[data-compte]").text(n ? n + " " + __("article(s) coché(s)") + " — " + __("environ") + " " + Math.max(1, Math.ceil(n / 4 * 25 / 60)) + " min" : "");
	}

	/** Fenêtre pour prendre des photos : celles d'un autre article, ou des fichiers déposés. */
	choisir_sources(prendre) {
		const esc = this.esc;
		const d = new frappe.ui.Dialog({
			title: __("Ajouter une photo source"),
			size: "large",
			fields: [
				{ fieldtype: "Link", fieldname: "article", options: "Item", label: __("Prendre des images d’un article"),
					onchange: () => {
						const code = d.get_value("article");
						const $z = d.fields_dict.dispo.$wrapper;
						if (!code) { $z.empty(); return; }
						frappe.call({ method: this.M + "images_de", args: { item_code: code } }).then((r) => {
							const images = r.message || [];
							$z.html(images.length ? `<div style="color:var(--text-muted);margin-bottom:6px">${__("Cliquez sur une image pour la prendre.")}</div>
								<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:8px">${images.map((x) => `
									<div data-prendre="${esc(x.name)}" data-url="${esc(x.file_url)}" data-de="${esc(code)}" style="border:1px solid var(--border-color);border-radius:8px;padding:4px;background:#fff;cursor:pointer">
										<img src="${esc(x.file_url)}" style="width:100%;height:96px;object-fit:contain" loading="lazy"></div>`).join("")}</div>`
								: `<div style="color:var(--text-muted)">${__("Cet article n’a aucune image.")}</div>`);
						});
					} },
				{ fieldtype: "HTML", fieldname: "dispo" },
				{ fieldtype: "Button", fieldname: "deposer", label: __("Ajouter mes propres images"),
					click: () => new frappe.ui.FileUploader({
						allow_multiple: true,
						restrictions: { allowed_file_types: ["image/*"] },
						on_success: (f) => prendre({ name: f.name, file_url: f.file_url, de: __("image déposée") }),
					}) },
			],
			primary_action_label: __("Terminé"),
			primary_action: () => d.hide(),
		});
		d.$wrapper.on("click", "[data-prendre]", (e) => {
			const $v = $(e.currentTarget);
			prendre({ name: $v.attr("data-prendre"), file_url: $v.attr("data-url"), de: $v.attr("data-de") });
			$v.css("opacity", 0.4);
		});
		d.show();
	}

	lancer() {
		const lignes = this.lignes.filter((l) => l.coche).map((l) => {
			const noms = l.sources.concat(this.communes).map((s) => s.name);
			return { article: l.name, sources: noms.filter((n, i) => noms.indexOf(n) === i).slice(0, this.MAX) };
		}).filter((l) => l.sources.length);
		if (!lignes.length) {
			frappe.show_alert({ message: __("Cochez au moins un article qui a des photos sources."), indicator: "orange" }, 5);
			return;
		}
		if (lignes.length > 60) {
			frappe.msgprint(__("Soixante articles au plus par lot : décochez-en quelques-uns."));
			return;
		}
		frappe.call({ method: this.M + "lancer", args: { lignes: lignes, consigne: this.consigne || "" } }).then((r) => {
			this.lot = (r.message || {}).lot;
			this.$corps.find("[data-zone=resultats]").empty();
			this.suivre(r.message);
		});
	}

	suivre(e) {
		const $a = this.$corps.find("[data-zone=avancement]");
		clearTimeout(this._minuterie);
		if (!e) { $a.hide(); return; }
		if (e.etat === "en_cours") {
			const part = e.total ? Math.round(100 * (e.faits || 0) / e.total) : 0;
			$a.show().html(`<div style="font-weight:700">🔄 ${__("Composition en cours")} — ${e.faits || 0} ${__("sur")} ${e.total} ${__("image(s)")}</div>
				<div style="height:6px;background:#dbeafe;border-radius:999px;margin-top:8px;overflow:hidden"><div style="height:100%;width:${Math.max(3, part)}%;background:#2563eb;border-radius:999px;transition:width .6s"></div></div>
				<div style="margin-top:6px;opacity:.85">${__("Vous pouvez quitter cette page : le travail se poursuit en arrière-plan.")}</div>`);
			this._minuterie = setTimeout(() => frappe.call({ method: this.M + "etat", args: { lot: this.lot } }).then((r) => this.suivre(r.message)), 3000);
			return;
		}
		$a.hide();
		if (e.etat === "echec") {
			frappe.msgprint({ title: __("La composition a échoué"), message: this.esc(e.message || ""), indicator: "red" });
			return;
		}
		if (e.etat === "a_valider") this.dessiner_resultats(e);
	}

	dessiner_resultats(e) {
		const esc = this.esc;
		const resultats = Object.values(e.resultats || {});
		const reussis = resultats.filter((r) => !r.echec), rates = resultats.filter((r) => r.echec);
		this.$corps.find("[data-zone=resultats]").html(`
			<div style="border:1px solid #fdba74;background:#fff7ed;border-radius:10px;padding:10px 12px">
				<div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center">
					<div style="flex:1 1 260px;font-weight:700;color:#9a3412">🖼️ ${reussis.length} ${__("image(s) composée(s) : à vérifier avant de les garder")}</div>
					<button class="btn btn-xs btn-default" data-cocher-resultats>${__("Tout garder")}</button>
					<label style="margin:0;cursor:pointer"><input type="checkbox" data-envoyer style="margin:0 6px 0 0">${__("Puis envoyer ces articles sur le site")}</label>
					<button class="btn btn-sm btn-primary" data-valider>${__("Valider mon choix")}</button>
				</div>
				${rates.length ? `<div style="margin-top:6px;color:#991b1b">⚠️ ${rates.length} ${__("image(s) non composée(s)")} : ${esc(rates[0].echec || "")} (${rates.map((r) => esc(r.article)).join(", ")})</div>` : ""}
				<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px;margin-top:12px">
				${reussis.map((r) => `
					<div style="border:1px solid var(--border-color);border-radius:10px;padding:8px;background:var(--card-bg,#fff)">
						<div style="font-weight:700">${esc(r.article)}</div>
						<div style="font-size:12px;margin-bottom:6px">${esc(r.nom || "")}</div>
						<a href="${esc(r.proposition_url)}" target="_blank" title="${__("Ouvrir en grand")}"><img src="${esc(r.proposition_url)}" style="width:100%;border:1px solid var(--border-color);border-radius:8px;background:#fff" loading="lazy"></a>
						<div style="margin-top:6px">${(r.sources || []).map((u) => `<img src="${esc(u)}" style="width:44px;height:44px;object-fit:contain;border:1px solid var(--border-color);border-radius:6px;background:#fff;margin-right:4px" loading="lazy">`).join("")}</div>
						<label style="display:flex;align-items:center;gap:8px;margin:8px 0 0;font-weight:700;cursor:pointer"><input type="checkbox" data-garder="${esc(r.article)}" style="margin:0"> ${__("Garder cette image")}</label>
					</div>`).join("")}
				</div>
			</div>`);
	}

	valider() {
		const gardees = this.$corps.find("[data-garder]:checked").map((i, el) => $(el).attr("data-garder")).get();
		const envoyer = this.$corps.find("[data-envoyer]").is(":checked") ? 1 : 0;
		const faire = () => frappe.call({ method: this.M + "valider", args: { lot: this.lot, gardees: gardees, synchroniser: envoyer }, freeze: true, freeze_message: __("Mise en place des images…") })
			.then((r) => {
				const m = r.message || {};
				frappe.msgprint({ title: __("Images mises en place"), indicator: "green",
					message: `${m.ajoutees || 0} ${__("image(s) ajoutée(s) aux articles")}, ${m.jetees || 0} ${__("refusée(s)")}` + (m.envoyees ? `, ${m.envoyees} ${__("article(s) en cours d’envoi sur le site")}` : "") });
				this.$corps.find("[data-zone=resultats]").empty();
				this.lot = null;
				this.charger();
			});
		if (!gardees.length) frappe.confirm(__("Aucune image n’est cochée : elles seront toutes jetées. Continuer ?"), faire);
		else faire();
	}
}
