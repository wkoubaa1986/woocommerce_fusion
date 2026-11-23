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

	def test_retouche_manuelle_puis_synchronisation(self):
		self.assertEqual(S.etapes_retouche({}, synchroniser=True), ["retouche", "site", "fiche", "images_site", "finitions"])
		self.assertEqual(S.etapes_retouche({}, synchroniser=False), ["retouche"])
		# « Générer SEO » coché : la synchronisation retouche ensuite les images NON traitées, c'est une autre étape
		self.assertEqual(S.etapes_retouche({"custom_generate_seo": 1}, True)[:4], ["retouche", "site", "textes", "images"])

	def test_le_bandeau_dit_ce_qui_a_ete_lance(self):
		self.assertEqual(S.LIBELLES["retouche"]["fait"], "Images retouchées")
		self.assertEqual(S.LIBELLES["synchro"]["titre"], "Synchronisation avec le site")

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
		self.assertLessEqual(len(C.consigne_complete("x" * 5000)) - len(C.EDIT_PROMPT), C.CONSIGNE_MAX + 500)

	def test_la_consigne_part_avec_l_image(self):
		client = mock.Mock()
		client.models.generate_content.return_value = _Reponse(_image())
		with mock.patch.object(C, "_read_gemini_model", return_value="modele"):
			C._gemini_retoucher(client, _image(), "montre le produit de face")
		demande = client.models.generate_content.call_args.kwargs["contents"][1]
		self.assertIn("montre le produit de face", demande)

	def test_le_passage_automatique_ne_retraite_jamais_une_image_traitee(self):
		"""« Lorsque c'est traité par l'IA on ne la retraite plus » : seule la retouche manuelle y revient."""
		import inspect
		source = inspect.getsource(C.treat_left_item_images)
		self.assertIn("if treated == 1:\n            continue", source)
		self.assertNotIn("consigne", source)
		self.assertIn("_gemini_retoucher(gemini_client, old_doc.get_content(), consigne)", inspect.getsource(C.retraiter_images))

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
		self.assertIn('this.METHODE + "lancer_retouche"', js)                       # la retouche manuelle avec consigne
		self.assertIn("fieldname: \"consigne\"", js)

	def test_bibliotheque_google_bornee(self):
		"""Sans borne, chaque reconstruction de la prod prenait la dernière version majeure."""
		with open(os.path.join(os.path.dirname(RACINE), "requirements.txt"), encoding="utf-8") as f:
			self.assertIn("google-genai>=2.0,<4", f.read().split())


if __name__ == "__main__":
	unittest.main()
