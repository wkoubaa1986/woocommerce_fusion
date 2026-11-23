"""Synchronisation manuelle en tâche de fond, avancement sur la fiche, retouche d'image (demandes du 23/11/2026).

`unittest.TestCase` pur : rien n'est écrit en base, Google et WordPress sont remplacés par des doublures.
Lancer dans le contexte du site (jamais `bench run-tests` sur le site de dev).
"""
import io
import os
import unittest
from unittest import mock

from PIL import Image

from woocommerce_fusion.integrations import content_enrichment as C
from woocommerce_fusion.integrations import wp_media
from woocommerce_fusion.tasks import suivi_synchro as S

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _image(couleur=(200, 30, 30), taille=(40, 30)) -> bytes:
	tampon = io.BytesIO()
	Image.new("RGB", taille, couleur).save(tampon, "PNG")
	return tampon.getvalue()


class _Reponse:
	"""Une réponse de Gemini portant une image (ou rien)."""

	def __init__(self, octets=None):
		partie = mock.Mock(text=None)
		partie.inline_data = mock.Mock(mime_type="image/png", data=octets) if octets else None
		self.candidates = [mock.Mock(content=mock.Mock(parts=[partie]), finish_reason="STOP")]
		self.prompt_feedback = None


class TestEtapes(unittest.TestCase):
	def test_article_simple_trois_etapes(self):
		self.assertEqual(S.etapes_prevues({}), ["site", "fiche", "images_site", "finitions"])

	def test_generer_seo_ajoute_textes_et_images(self):
		self.assertEqual(S.etapes_prevues({"custom_generate_seo": 1}), ["site", "textes", "images", "fiche", "images_site", "finitions"])

	def test_classement_seulement_hors_variante(self):
		self.assertEqual(S.etapes_prevues({"custom_generate_tag": 1})[:2], ["site", "classement"])
		self.assertNotIn("classement", S.etapes_prevues({"custom_generate_tag": 1, "variant_of": "Modele"}))

	def test_apres_validation_mise_en_place_puis_synchronisation(self):
		self.assertEqual(S.etapes_apres("application", {}, True), ["application", "site", "fiche", "images_site", "finitions"])
		self.assertEqual(S.etapes_apres("application", {}, False), ["application"])
		self.assertEqual(S.etapes_apres("restauration", {"custom_generate_seo": 1}, True)[:4], ["restauration", "site", "textes", "images"])

	def test_le_bandeau_dit_ce_qui_a_ete_lance(self):
		self.assertEqual(S.LIBELLES["retouche"]["fait"], "Retouches prêtes")
		self.assertEqual(S.LIBELLES["synchro"]["titre"], "Synchronisation avec le site")
		self.assertEqual(set(S.LIBELLES), {"synchro", "retouche", "composition", "validation", "restauration"})

	def test_a_valider_n_est_pas_en_cours(self):
		"""Des retouches qui attendent le choix de l'utilisateur ne bloquent rien et ne sont jamais « interrompues »."""
		self.assertNotIn(S.A_VALIDER, S.EN_COURS)
		self.assertFalse(S.interrompue({"etat": S.A_VALIDER, "maj": "2026-11-20 08:00:00"}, "2026-11-23 20:00:00"))

	def test_avancer_donne_le_rang(self):
		e = {"item": "A", "etapes": ["textes", "images", "fiche", "images_site", "finitions"]}
		e = S.avancer(e, "images", "image 2 sur 4")
		self.assertEqual((e["etat"], e["rang"], e["total"], e["libelle"], e["detail"]),
						 ("en_cours", 2, 5, "Retouche des images", "image 2 sur 4"))

	def test_une_etape_imprevue_s_ajoute(self):
		e = S.avancer({"item": "A", "etapes": ["fiche"]}, "classement")
		self.assertEqual((e["etapes"], e["rang"], e["total"]), (["fiche", "classement"], 2, 2))

	def test_interrompue_apres_trente_minutes_de_silence(self):
		e = {"etat": "en_cours", "maj": "2026-11-23 20:00:00"}
		self.assertFalse(S.interrompue(e, "2026-11-23 20:29:00"))
		self.assertTrue(S.interrompue(e, "2026-11-23 20:31:00"))
		self.assertFalse(S.interrompue(dict(e, etat="terminee"), "2026-11-23 23:00:00"))
		self.assertFalse(S.interrompue(None, "2026-11-23 23:00:00"))

	def test_cause_lisible(self):
		self.assertEqual(S.cause_lisible(ValueError("\n  Site injoignable \nTraceback…")), "Site injoignable")
		self.assertEqual(S.cause_lisible(KeyError()), "KeyError")

	def test_synchronisation_desactivee_dite_clairement(self):
		SyncDisabledError = type("SyncDisabledError", (Exception,), {})
		self.assertIn("désactivée", S.cause_lisible(SyncDisabledError("WooCommerceServer(site)")))

	def test_l_etat_est_relu_sans_copie_locale(self):
		"""Une valeur à durée limitée n'est pas remise à jour dans la copie locale de Frappe : il faut la contourner."""
		cache = mock.Mock()
		with mock.patch.object(S.frappe, "cache", return_value=cache):
			S._lire("A")
		cache.get_value.assert_called_once_with("wcf_synchro:A", expires=True)


class TestSuiviMuetHorsSynchroManuelle(unittest.TestCase):
	"""Synchro de masse, tâches planifiées : le code appelle le suivi sans condition, il ne doit rien faire."""

	def test_rien_sans_suivi(self):
		with mock.patch.object(S, "_suivi", return_value=None), mock.patch.object(S, "_ecrire") as ecrire:
			S.etape("images", "image 1 sur 2")
			S.avertir("x")
			S.noter("y")
			ecrire.assert_not_called()

	def test_etape_avertir_noter(self):
		etat = {"item": "A", "etapes": ["images", "fiche"], "avertissements": [], "resume": []}
		ecrits = []
		with mock.patch.object(S, "_suivi", return_value="A"), mock.patch.object(S, "_lire", side_effect=lambda i: dict(etat)), \
				mock.patch.object(S, "_ecrire", side_effect=lambda e: ecrits.append(e) or etat.update(e) or e):
			S.etape("images", "image 1 sur 2")
			S.avertir("1 image non retouchée")
			S.avertir("1 image non retouchée")          # jamais deux fois
			S.noter("1 image retouchée")
		self.assertEqual(etat["avertissements"], ["1 image non retouchée"])
		self.assertEqual(etat["resume"], ["1 image retouchée"])
		self.assertEqual((ecrits[0]["etape"], ecrits[0]["rang"]), ("images", 1))

	def test_le_suivi_ne_fait_jamais_echouer_la_synchro(self):
		with mock.patch.object(S, "_suivi", return_value="A"), mock.patch.object(S, "_lire", side_effect=RuntimeError("cache en panne")):
			S.etape("images")
			S.avertir("x")


class TestRetouche(unittest.TestCase):
	def test_la_consigne_garde_les_cotes(self):
		p = C.EDIT_PROMPT
		self.assertIn("CASE A - PRODUCT PHOTO", p)
		self.assertIn("CASE B - TECHNICAL OR ANNOTATED IMAGE", p)
		self.assertIn("Keep EVERY dimension line, arrow, number, unit", p)
		self.assertIn("NEVER remove, move, translate, round, retype or invent", p)
		self.assertNotIn("3/4", p)                             # l'ancienne consigne refaisait l'image en trois-quarts
		self.assertIn("Keep the SAME viewpoint", p)

	def test_la_consigne_demande_la_qualite(self):
		for attendu in ("Improve the image quality", "remove noise, blur and JPEG compression", "increase the resolution",
						"pure white seamless studio background"):
			self.assertIn(attendu, C.EDIT_PROMPT, attendu)

	def test_service_en_v1beta(self):
		"""google-genai 3.x parle par défaut à v1, où le modèle « preview » du réglage n'existe pas (404 du 23/11/2026)."""
		self.assertEqual(C.GEMINI_API_VERSION, "v1beta")
		doc = mock.Mock(gemini_api_key="cle", google_api_key=None)
		with mock.patch.object(C, "_ai_settings_doctype", return_value="AI Settings"), \
				mock.patch.object(C.frappe, "get_cached_doc", return_value=doc), mock.patch.object(C.genai, "Client") as client, \
				mock.patch.object(C.frappe, "conf", {}):
			C._get_gemini_client()
		self.assertEqual(client.call_args.kwargs["http_options"].api_version, "v1beta")

	def test_retouche_reussie(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image((10, 200, 10)))
		with mock.patch.object(C, "_read_gemini_model", return_value="modele"):
			octets, ok, raison = C._gemini_retoucher(client, _image())
		self.assertTrue(ok)
		self.assertEqual(raison, "")
		self.assertEqual(Image.open(io.BytesIO(octets)).size, (2000, 2000))
		self.assertEqual(client.models.generate_content.call_count, 1)

	def test_modele_introuvable_echec_dit_et_image_d_origine(self):
		client = mock.Mock()
		client.models.generate_content.side_effect = RuntimeError("404 NOT_FOUND. models/x is not found for API version v1")
		with mock.patch.object(C, "_read_gemini_model", return_value="modele"), mock.patch.object(C.frappe, "log_error"), \
				mock.patch.object(C.frappe, "get_traceback", return_value="trace"):
			octets, ok, raison = C._gemini_retoucher(client, _image((200, 30, 30)))
		self.assertFalse(ok)
		self.assertIn("introuvable", raison)
		self.assertEqual(client.models.generate_content.call_count, 1)          # inutile de réessayer sans les options
		self.assertEqual(Image.open(io.BytesIO(octets)).size, (2000, 2000))

	def test_options_refusees_la_retouche_est_retentee_sans(self):
		client = mock.Mock()
		client.models.generate_content.side_effect = [RuntimeError("400 INVALID_ARGUMENT image_size"), _Reponse(_image())]
		with mock.patch.object(C, "_read_gemini_model", return_value="modele"), mock.patch.object(C.frappe, "log_error"), \
				mock.patch.object(C.frappe, "get_traceback", return_value="trace"):
			_octets, ok, _raison = C._gemini_retoucher(client, _image())
		self.assertTrue(ok)
		self.assertEqual(client.models.generate_content.call_count, 2)
		self.assertIn("config", client.models.generate_content.call_args_list[0].kwargs)
		self.assertNotIn("config", client.models.generate_content.call_args_list[1].kwargs)

	def test_aucune_image_renvoyee(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(None)
		with mock.patch.object(C, "_read_gemini_model", return_value="modele"), mock.patch.object(C.frappe, "log_error"):
			_octets, ok, raison = C._gemini_retoucher(client, _image())
		self.assertFalse(ok)
		self.assertIn("aucune image", raison)

	def test_raisons(self):
		self.assertIn("clé Gemini", C.raison_echec_retouche("403 PERMISSION_DENIED"))
		self.assertIn("quota", C.raison_echec_retouche("429 RESOURCE_EXHAUSTED"))
		self.assertEqual(C.raison_echec_retouche("Connexion coupée\nsuite"), "Connexion coupée")

	def test_sans_consigne_la_consigne_generale(self):
		self.assertEqual(C.consigne_complete(None), C.EDIT_PROMPT)
		self.assertEqual(C.consigne_complete("   "), C.EDIT_PROMPT)

	def test_la_consigne_de_l_utilisateur_s_ajoute(self):
		p = C.consigne_complete("  enlève le reflet\n sur le capot  ")
		self.assertTrue(p.startswith(C.EDIT_PROMPT))
		self.assertIn('"""enlève le reflet sur le capot"""', p)
		self.assertIn("It has priority over the framing, background and lighting rules", p)
		self.assertIn("nor to remove or alter any dimension, number or label", p)          # les cotes restent protégées
		longue = C.consigne_complete("x" * 5000)                 # une consigne trop longue est coupée
		self.assertIn("x" * C.CONSIGNE_MAX, longue)
		self.assertNotIn("x" * (C.CONSIGNE_MAX + 1), longue)

	def test_la_consigne_part_avec_l_image(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image())
		with mock.patch.object(C, "_read_gemini_model", return_value="modele"), mock.patch.object(C.frappe, "log_error"):
			C._gemini_retoucher(client, _image(), "montre le produit de face")
		demande = client.models.generate_content.call_args.kwargs["contents"][1]
		self.assertIn("montre le produit de face", demande)

	def test_le_passage_automatique_ne_retraite_jamais_une_image_traitee(self):
		"""« Lorsque c'est traité par l'IA on ne la retraite plus » : seule la retouche manuelle y revient."""
		import inspect
		source = inspect.getsource(C.treat_left_item_images)
		self.assertIn("if treated == 1:\n            continue", source)
		self.assertNotIn("consigne", source)

	def test_la_consigne_n_invente_rien(self):
		"""Retouche fausse du 23/11/2026 : des cotes dessinées sur une photo qui n'en avait pas, un gris devenu blanc."""
		self.assertIn("NEVER add dimension lines, arrows, numbers, labels or any text that are not in the original photo", C.EDIT_PROMPT)
		self.assertIn("NEVER change the colors or the materials of the product", C.EDIT_PROMPT)
		p = C.consigne_complete("écris les cotes en rouge")
		self.assertIn("If the instruction mentions something that does not exist in THIS image", p)
		self.assertIn("never draw it, never invent", p)

	def test_la_retouche_manuelle_ne_remplace_rien_avant_validation(self):
		import inspect
		proposer = inspect.getsource(C.proposer_retouches)
		self.assertNotIn("_remplacer_par_retouche", proposer)                 # premier temps : des propositions seulement
		self.assertIn('"doctype": "File", "file_name": "proposition-', proposer)   # mises de côté, jointes à rien
		self.assertIn("_remplacer_par_retouche", inspect.getsource(C.appliquer_retouches))
		self.assertIn('"etat": A_VALIDER', inspect.getsource(S.executer_propositions))

	def test_une_consigne_par_image(self):
		import inspect
		self.assertIn("consigne_complete(consignes.get(nom))", inspect.getsource(C.proposer_retouches))
		self.assertIn("consignes = {f: str(consignes.get(f) or", inspect.getsource(S.lancer_retouche))

	def test_plusieurs_retouches_en_meme_temps(self):
		import inspect
		self.assertIn("ThreadPoolExecutor(max_workers=min(RETOUCHES_EN_PARALLELE", inspect.getsource(C.proposer_retouches))
		self.assertEqual(C.RETOUCHES_EN_PARALLELE, 4)
		# la retouche qui tourne dans un fil à part ne touche à rien de Frappe
		pure = inspect.getsource(C._retouche_pure)
		self.assertNotIn("frappe.", pure.split('"""', 2)[2])

	def test_l_image_d_avant_est_notee(self):
		import inspect
		self.assertEqual(C.CHAMP_IMAGE_AVANT, "custom_image_avant")
		self.assertIn("CHAMP_IMAGE_AVANT, old_id", inspect.getsource(C._remplacer_par_retouche))

	def test_l_ancienne_entree_rend_toujours_des_octets(self):
		with mock.patch.object(C, "_gemini_retoucher", return_value=(b"x", False, "raison")):
			self.assertEqual(C._gemini_edit_image_bytes(None, b""), b"x")


class TestSuppressionDesAnciennesImages(unittest.TestCase):
	"""L'appelant passe une LISTE : `.keys()` levait, et rien n'était jamais supprimé du site."""

	def _supprimer(self, identifiants):
		session = mock.Mock()
		session.delete.return_value = mock.Mock(status_code=200, text="")
		with mock.patch.object(wp_media, "_get_wp_config", return_value=mock.Mock(base_url="https://site")), \
				mock.patch.object(wp_media, "_new_wp_session", return_value=session):
			return wp_media.delete_media_by_ids(identifiants), session

	def test_une_liste(self):
		r, session = self._supprimer([12, 34])
		self.assertEqual(r["deleted"], [12, 34])
		self.assertEqual(session.delete.call_count, 2)

	def test_un_dictionnaire_comme_avant(self):
		r, _ = self._supprimer({12: "a"})
		self.assertEqual(r["deleted"], [12])

	def test_rien(self):
		r, session = self._supprimer([])
		self.assertEqual((r["deleted"], session.delete.call_count), ([], 0))


class TestFicheEtDependances(unittest.TestCase):
	def test_le_bouton_passe_par_la_tache_de_fond(self):
		with open(os.path.join(RACINE, "public", "js", "stock", "item.js"), encoding="utf-8") as f:
			js = f.read()
		self.assertIn('METHODE: "woocommerce_fusion.tasks.suivi_synchro."', js)
		self.assertNotIn("woocommerce_fusion.tasks.sync_items.run_item_sync", js)      # plus d'appel direct, écran gelé
		self.assertIn("wcf_synchro.suivre(frm)", js)
		self.assertIn('this.METHODE + "lancer_retouche"', js)                       # la retouche manuelle
		self.assertIn("data-wcf-consigne=", js)                                     # une consigne par image
		self.assertIn('this.METHODE + "valider_retouches"', js)                     # le contrôle avant
		self.assertIn('this.METHODE + "restaurer"', js)                             # revenir à l'image d'avant

	def test_bibliotheque_google_bornee(self):
		"""Sans borne, chaque reconstruction de la prod prenait la dernière version majeure."""
		with open(os.path.join(os.path.dirname(RACINE), "requirements.txt"), encoding="utf-8") as f:
			self.assertIn("google-genai>=2.0,<4", f.read().split())



class TestModelesParEtape(unittest.TestCase):
	"""« Remets tout en setting » : chaque étape lit son modèle dans AI Settings."""

	def test_temperature_seulement_pour_les_anciens_modeles(self):
		for accepte in ("gpt-4o-mini", "gpt-4.1-mini", "GPT-4o"):
			self.assertTrue(C.accepte_temperature(accepte), accepte)
		# vérifié avec la clé du site le 23/11/2026 : « Only the default (1) value is supported »
		for refuse in ("gpt-6.2-sol", "gpt-6.1-terra", "gpt-6-luna", "gpt-5-mini", "gpt-5.2", ""):
			self.assertFalse(C.accepte_temperature(refuse), refuse)
		self.assertTrue(C._is_gpt5("gpt-6-luna"))

	def test_le_reglage_passe_avant_le_modele_d_avant(self):
		with mock.patch.object(C, "_read_ai_setting", return_value=" gpt-6-luna "):
			self.assertEqual(C.modele_etape("keywords_model"), "gpt-6-luna")
		with mock.patch.object(C, "_read_ai_setting", return_value=None):
			self.assertEqual(C.modele_etape("keywords_model"), "gpt-5-mini")
			self.assertEqual(C.modele_etape("web_search_model"), "gpt-4.1-mini")
		with mock.patch.object(C, "_read_ai_setting", side_effect=RuntimeError("champ absent")):
			self.assertEqual(C.modele_etape("alt_model"), "gpt-4o-mini")

	def test_plus_de_modele_ecrit_en_dur_dans_les_etapes(self):
		import inspect
		source = inspect.getsource(C)
		for attendu in ('model_key = modele_etape("keywords_model")', 'modele_etape("logistics_model")',
						'model_name = modele_etape("alt_model")', 'WEB_MODEL = modele_etape("web_search_model")'):
			self.assertIn(attendu, source, attendu)

	def test_reglages_poses(self):
		from woocommerce_fusion.setup import reglages_ia as R
		self.assertEqual(R.MODELES["open_ai_model"][1:3], ("gpt-6.2-sol", ("gpt-5.2",)))
		self.assertEqual({R.MODELES[c][1] for c in R.NOUVEAUX}, {"gpt-6-luna"})
		self.assertEqual(set(R.NOUVEAUX), set(C.MODELES_PAR_ETAPE))



class TestRangementDesImages(unittest.TestCase):
	"""Ordre des images, image principale, images gardées hors du site (demandes du 23/11/2026)."""

	def test_sans_rang_rien_ne_change(self):
		self.assertIsNone(C.ranger([{"name": "a", "creation": "2026-01-01"}, {"name": "b", "custom_ordre_image": 0}]))

	def test_l_ordre_choisi_fait_foi(self):
		rows = [{"name": "a", "custom_ordre_image": 3, "creation": "2026-01-01"}, {"name": "b", "custom_ordre_image": 1, "creation": "2026-03-01"},
				{"name": "c", "custom_ordre_image": 2, "creation": "2026-02-01"}]
		self.assertEqual([r["name"] for r in C.ranger(rows)], ["b", "c", "a"])

	def test_une_image_jamais_rangee_suit_les_autres_par_date(self):
		rows = [{"name": "nouvelle2", "creation": "2026-11-02"}, {"name": "a", "custom_ordre_image": 1, "creation": "2026-01-01"},
				{"name": "nouvelle1", "custom_ordre_image": 0, "creation": "2026-11-01"}]
		self.assertEqual([r["name"] for r in C.ranger(rows)], ["a", "nouvelle1", "nouvelle2"])

	def test_l_ordre_choisi_passe_avant_l_image_principale_d_origine(self):
		it = mock.Mock(image="/files/x.jpg")
		rows = [{"name": "x", "file_url": "/files/x.jpg", "attached_to_field": "image", "custom_ordre_image": 2, "creation": "1"},
				{"name": "y", "file_url": "/files/y.jpg", "custom_ordre_image": 1, "creation": "2"}]
		self.assertEqual([r["name"] for r in C._make_main_first(it, rows)], ["y", "x"])
		# sans rang : comme avant, l'image principale d'abord
		sans = [{"name": "y", "file_url": "/files/y.jpg"}, {"name": "x", "file_url": "/files/x.jpg", "attached_to_field": "image"}]
		self.assertEqual([r["name"] for r in C._make_main_first(it, sans)], ["x", "y"])

	def test_la_synchronisation_suit_l_ordre_et_ecarte_les_images_hors_site(self):
		import inspect
		from woocommerce_fusion.tasks import sync_items
		source = inspect.getsource(sync_items._upload_item_images_to_wp_and_attach)
		self.assertIn("ranges = ranger(rows)", source)
		self.assertIn('if int(f.get("custom_hors_site") or 0) == 1:\n            continue', source)

	def test_une_retouche_garde_le_rang_de_l_image_qu_elle_remplace(self):
		import inspect
		self.assertIn("for champ in champs_de_rangement():\n        setattr(new_doc, champ, old_doc.get(champ) or 0)",
					  inspect.getsource(C._remplacer_par_retouche))
		self.assertEqual((C.CHAMP_ORDRE, C.CHAMP_HORS_SITE), ("custom_ordre_image", "custom_hors_site"))

	def test_la_fenetre_existe(self):
		with open(os.path.join(RACINE, "public", "js", "stock", "item.js"), encoding="utf-8") as f:
			js = f.read()
		self.assertIn('this.METHODE + "enregistrer_images"', js)
		self.assertIn("data-wcf-hors", js)
		self.assertIn('__("Images : ordre et site…")', js)



class TestRetoucheDUneZone(unittest.TestCase):
	"""« Sélectionner une zone et donner une consigne claire, là-dedans, à améliorer » (23/11/2026)."""

	def test_zone_valide(self):
		self.assertEqual(C.zone_valide([0.1, 0.2, 0.3, 0.4]), (0.1, 0.2, 0.3, 0.4))
		self.assertEqual(C.zone_valide(["0.8", 0.9, 0.5, 0.5]), (0.8, 0.9, 0.2, 0.1))        # bornée à l'image
		self.assertIsNone(C.zone_valide([0.1, 0.1, 0.01, 0.5]))                                # trop petite
		self.assertIsNone(C.zone_valide([0, 0, 1, 1]))                                         # toute l'image
		self.assertIsNone(C.zone_valide("n'importe quoi"))
		self.assertIsNone(C.zone_valide(None))

	def test_boites(self):
		zone, envoi = C.boites_de_zone((0.25, 0.25, 0.5, 0.5), 2000, 2000)
		self.assertEqual(zone, (500, 500, 1500, 1500))
		self.assertEqual(envoi, (380, 380, 1620, 1620))                    # 12 % de marge autour
		_zone, envoi = C.boites_de_zone((0.0, 0.0, 0.5, 0.5), 2000, 2000)
		self.assertEqual(envoi[:2], (0, 0))                                 # jamais hors de l'image

	def test_hors_de_la_zone_l_image_reste_identique_au_pixel_pres(self):
		origine = Image.new("RGB", (400, 300), (10, 120, 200))
		retouche = Image.new("RGB", (512, 512), (250, 20, 20))             # ce que l'IA rend : un carré tout rouge
		zone = (0.5, 0.5, 0.25, 0.3)
		resultat = C.recoller_zone(origine, retouche, zone)
		self.assertEqual(resultat.size, origine.size)
		(g, h, d, b), (eg, eh, ed, eb) = C.boites_de_zone(zone, 400, 300)
		self.assertEqual(resultat.getpixel(((g + d) // 2, (h + b) // 2)), (250, 20, 20))       # le cœur de la zone a changé
		for point in ((5, 5), (399, 5), (5, 299), (eg - 2, eh - 2), (g // 2, h // 2)):          # loin de la zone : rien
			self.assertEqual(resultat.getpixel(point), (10, 120, 200), point)

	def test_la_retouche_de_zone_rend_l_image_entiere(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image((250, 20, 20), (64, 64)))
		octets, ok, raison, journal = C._retouche_zone_pure(client, "modele", "enlève la tache", _image((10, 120, 200), (300, 300)), (0.4, 0.4, 0.2, 0.2))
		self.assertTrue(ok)
		sortie = Image.open(io.BytesIO(octets)).convert("RGB")
		self.assertEqual(sortie.size, (2000, 2000))
		coin = sortie.getpixel((20, 20))
		self.assertTrue(abs(coin[0] - 10) < 12 and abs(coin[2] - 200) < 12, coin)               # le coin est resté bleu
		envoye, demande = client.models.generate_content.call_args.kwargs["contents"]
		self.assertEqual(envoye.width, envoye.height)                                           # la zone part au carré
		self.assertLess(envoye.width, 300)                                                      # la zone seulement
		self.assertIn("enlève la tache", demande)
		self.assertIn("Only this area must be improved", demande)

	def test_zone_sans_consigne(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image())
		C._retouche_zone_pure(client, "modele", "", _image((1, 2, 3), (200, 200)), (0.1, 0.1, 0.5, 0.5))
		self.assertIn(C.CONSIGNE_ZONE_DEFAUT, client.models.generate_content.call_args.kwargs["contents"][1])

	def test_la_fenetre_trace_la_zone(self):
		with open(os.path.join(RACINE, "public", "js", "stock", "item.js"), encoding="utf-8") as f:
			js = f.read()
		self.assertIn("choisir_zone(url, zone, suite)", js)
		self.assertIn("zones: zones", js)
		self.assertIn("pointerdown", js)



class TestComposerUneImage(unittest.TestCase):
	"""« Les articles qui n'ont pas d'image : recomposer l'image en prenant plusieurs images » (23/11/2026)."""

	def test_la_demande_porte_le_produit_et_la_consigne(self):
		d = C.demande_de_composition("Adoucisseur FRP 1035, 25 L", "<p>Tête <b>Runxin F65B3</b></p>", "la vanne sur la bouteille", 3)
		self.assertIn("You receive 3 reference photo(s)", d)
		self.assertIn("Product: Adoucisseur FRP 1035, 25 L", d)
		self.assertIn("Description: Tête Runxin F65B3", d)
		self.assertIn('"""la vanne sur la bouteille"""', d)
		self.assertIn("Use ONLY the parts shown in the reference photos", d)
		self.assertIn("Do NOT add any text, dimension line, number", d)

	def test_sans_consigne_ni_description(self):
		d = C.demande_de_composition("Bac", None, "", 1)
		self.assertIn(C.CONSIGNE_COMPOSITION_DEFAUT, d)
		self.assertNotIn("Description:", d)

	def test_toutes_les_photos_partent_avec_la_demande(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image((0, 200, 0), (80, 80)))
		octets, ok, raison, journal = C._composition_pure(client, "modele", "demande", [_image(), _image((1, 2, 3)), _image((9, 9, 9))])
		self.assertTrue(ok)
		self.assertEqual(Image.open(io.BytesIO(octets)).size, (2000, 2000))
		contenus = client.models.generate_content.call_args.kwargs["contents"]
		self.assertEqual(len(contenus), 4)                      # trois photos, puis la demande
		self.assertEqual(contenus[-1], "demande")

	def test_six_photos_au_plus(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image())
		C._composition_pure(client, "modele", "demande", [_image()] * 9)
		self.assertEqual(len(client.models.generate_content.call_args.kwargs["contents"]), C.COMPOSITION_SOURCES_MAX + 1)

	def test_echec_sans_image(self):
		client = mock.Mock()
		client.models.generate_content.side_effect = RuntimeError("429 RESOURCE_EXHAUSTED")
		octets, ok, raison, journal = C._composition_pure(client, "modele", "demande", [_image()])
		self.assertEqual((octets, ok), (None, False))
		self.assertIn("quota", raison)

	def test_une_composition_s_ajoute_une_retouche_remplace(self):
		self.assertEqual(C.cle_de_proposition({"ancien": "f1", "proposition": "p1"}), "f1")
		self.assertEqual(C.cle_de_proposition({"ancien": None, "proposition": "p1", "composition": 1}), "p1")
		import inspect
		source = inspect.getsource(C.appliquer_retouches)
		self.assertIn('if p.get("composition"):', source)
		self.assertIn("_ajouter_image_composee(it, octets)", source)

	def test_la_fenetre_existe(self):
		with open(os.path.join(RACINE, "public", "js", "stock", "item.js"), encoding="utf-8") as f:
			js = f.read()
		self.assertIn('this.METHODE + "lancer_composition"', js)
		self.assertIn("frappe.ui.FileUploader", js)              # ses propres images
		self.assertIn("data-wcf-prendre", js)                    # les images d'un autre article



class TestComposerDesImagesEnLot(unittest.TestCase):
	"""La page « Composer des images » : photos sources proposées, lot en arrière-plan, validation (23/11/2026)."""

	def test_pieces_d_un_adoucisseur_frp(self):
		p = C.pieces_probables("Adoucisseur FRP 1035, 25 L de résine, Runxin F65B3", "<ul><li>Bac à sel séparé de 70 L.</li></ul>", ["Runxin F65B3", "Sans bypass"])
		self.assertEqual(p, {"bouteille": "FRP-1035", "bac": "BT-70", "modeles": ["F65B3"]})

	def test_tete_keman_et_kit_bypass(self):
		p = C.pieces_probables("Adoucisseur FRP 1054, 40 L de résine, Keman ASD4-LCD", "Bac à sel séparé de 70 L.", ["Keman ASD4-LCD", "Inclus dans la tête"])
		self.assertEqual((p["bouteille"], p["modeles"]), ("FRP-1054", ["ASD4-LCD"]))
		p = C.pieces_probables("Adoucisseur FRP 1035, 30 L, Runxin F63B3, Avec kit bypass", "Cette variante comprend le kit bypass Runxin F70A (Rn-41104).", ["Runxin F63B3", "Avec kit bypass"])
		self.assertEqual(p["modeles"][:2], ["F63B3", "F70A"])

	def test_article_sans_piece_reconnue(self):
		self.assertEqual(C.pieces_probables("Filtre à sédiments 5 microns", None, None), {"bouteille": None, "bac": None, "modeles": []})

	def test_la_bouteille_n_est_pas_prise_pour_une_vanne(self):
		self.assertNotIn("FRP1035", C.pieces_probables("Adoucisseur FRP1035", "", [])["modeles"])

	def test_un_lot_compose_plusieurs_articles_en_meme_temps(self):
		import inspect
		source = inspect.getsource(C.proposer_compositions_par_article)
		self.assertIn("ThreadPoolExecutor(max_workers=min(RETOUCHES_EN_PARALLELE", source)
		self.assertNotIn("attached_to_doctype", source)              # des propositions seulement, jointes à rien

	def test_la_page_et_son_serveur(self):
		from woocommerce_fusion.tasks import composition_lot as L
		for nom in ("articles", "images_de", "lancer", "etat", "valider"):
			self.assertTrue(callable(getattr(L, nom)), nom)
		self.assertEqual(L.LOT_MAX, 60)
		base = os.path.join(RACINE, "woocommerce", "page", "composer_images")
		with open(os.path.join(base, "composer_images.js"), encoding="utf-8") as f:
			js = f.read()
		for attendu in ('frappe.pages["composer-images"].on_page_load', 'this.M + "lancer"', 'this.M + "valider"', "data-garder"):
			self.assertIn(attendu, js, attendu)
		import json
		with open(os.path.join(base, "composer_images.json"), encoding="utf-8") as f:
			page = json.load(f)
		self.assertEqual((page["name"], page["module"], page["standard"]), ("composer-images", "WooCommerce", "Yes"))


if __name__ == "__main__":
	unittest.main()
