# Principaux raccourcis

- **Keyframes-Extractor (MIT, Thales MMS)** – Outil Python qui détecte automatiquement les écrans stables dans une vidéo de bureau, en ne gardant qu’une capture par scène. Il génère des images clefs JPEG avec horodatages, supprimant les quasi-duplications. Ce composant prend en entrée un fichier vidéo (MP4) et produit des images horodatées. Il nous épargne de coder la détection de changement d’écran manuel.  
- **OmniParser (CC-BY-4.0, Microsoft)** – Framework complet de parsing visuel d’IU. Il extrait des éléments d’interface (zones cliquables, icônes, texte) et produit une structure (JSON) détaillée pour chaque capture d’écran. Son code (Python/YOLOv9) est complexe mais fournit des descriptions d’écran « prêtes à consommer » pour un modèle multimodal. Il couvre ainsi presque toute l’étape de *screenshot → descriptions structurées* (étape 2 du pipeline), détectant boutons, champs et libellés. À condition d’installer les modèles (clé YOLOv9 et poids de caption), il enlève le besoin de bâtir notre propre analyseur d’interface.  
- **Screex (MIT, BlueprintParadise)** – Outil en ligne de commande qui indexe un enregistrement d’écran en états d’écran (UI states) et texte OCR. Pour chaque état (p. ex. « écran de connexion », « tableau de bord »), Screex enregistre l’image et extrait le texte visible, puis note ce qui apparaît ou disparaît entre états. Le résultat est un transcript horodaté de « captures d’écran + transitions » (flux texte, captures JPEG, etc.). Cela couvre les étapes 1–3 : détection d’images clefs, OCR, et inférence des transitions (via différences textuelles). Il nécessite Python (≥3.9) et les modèles OCR ONNX intégrés, et produit un index JSON. Ce pipeline prêt à l’emploi nous évite d’écrire la logique de segmentation d’écran et d’extraction de texte.  
- **Video-to-SOP (MIT ?)** – Application Streamlit « video2sop » développée pour convertir un enregistrement en procédure pas-à-pas. Elle combine OpenCV, détection de seuils adaptatifs, OCR Tesseract, et (optionnellement) GPT pour générer un document Word (.docx) structuré. Selon la doc, elle effectue l’extraction dense d’images, la segmentation des états d’écran, l’OCR et une mise en forme finale avec captures et textes. Ce projet couvre l’ensemble des étapes 1–4 : keyframes, description, transitions et groupement en étapes. Son code publié inclut tout le pipeline (OpenCV, python-docx, etc.) – on peut s’en inspirer directement pour gagner du temps, quitte à désactiver l’API OpenAI pour un prototype simple.  
- **OpenAdapt Capture (MIT)** – Bibliothèque Python pour l’enregistrement instrumenté de bureau. Plutôt que d’analyser une vidéo brute, OpenAdapt *capturerait en temps réel* les clics/saisies et prend des screenshots en synchronisation. Elle produit un dossier avec base de données SQLite d’actions (clics, touches, etc.), accès d’UI (accessibilité) et vidéo. Pour la pile instrumentée, c’est un raccourci énorme : on récupère immédiatement la liste des événements utilisateur et l’image exacte à chaque action, sans besoin d’algorithmes de vision pour deviner les transitions. Il faut installer FFmpeg (pour la vidéo) et permissions système, mais cela remplace entièrement les étapes 1–3 en aval. 

Chaque projet ci-dessus est open-source et (sauf OmniParser très lourd) peut être intégré sans partir de zéro. Par exemple, *Keyframes-Extractor* élimine la phase “écrire son propre code de détection de changement d’écran”. *OmniParser* ou *Video-to-SOP* offrent des solutions multiplateformes tout-en-un couvrant plusieurs étapes du pipeline (screenshot→structure ou vidéo→procédure). *Screex* apporte un workflow clé-en-main centré sur l’OCR et l’indexation d’état. Enfin, *OpenAdapt Capture* est un raccourci complet pour l’architecture instrumentée : plutôt que de décortiquer une vidéo, on « scande » l’activité en live. 

# Tableau comparatif des composants par étape

| Étape du pipeline                | Projet / Librairie                    | Licence   | Entrées principales               | Sorties clés                    | Vérification / Observations                              |
|----------------------------------|---------------------------------------|-----------|-----------------------------------|---------------------------------|---------------------------------------------------------|
| **Extraction de frames / keyframes** | Keyframes-Extractor (Python CLI) | MIT       | Vidéo (MP4)                        | Images JPEG stables + timestamps | Documenté dans README (GitHub). Configure seuil. |
|                                  | videotoguide (Python CLI & UI) | MIT       | Vidéo                              | Images extraites (utile pour guiding manuel)  | (Noter: intègre PySceneDetect.)            |
|                                  | PySceneDetect (PyPI)   | MIT       | Vidéo                              | Changements de scène (frames)    | Externe, testé en tant que dépendance de videotoguide.      |
|                                  | video-to-keyframes (Python CLI) | Apache-2.0| Vidéo                              | Images clés (par SSIM/histogram) | Outil générique (pas spécifiquement UI). Documenté (GitHub).   |
|                                  | Katna (Python)                        | MIT       | Vidéo                              | Fps ou frames (clustering)      | Complexe (multiples étapes CV) – riche mais gros dépôt.     |
| **OCR & description d’écrans**   | OmniParser (Python/YOLO) | CC-BY-4.0 | Image screenshot                   | Arbre UI structuré JSON         | Solution ML avancée (icônes+régions interactives). Complexe à installer. |
|                                  | Deki (YOLO-based, Google-IPP)  | GPL-3.0   | Image screenshot                   | JSON : boîtes (`View`, `Text`, `Icon`) | Entraîné sur Android. Fournit détection de conteneurs et texte. |
|                                  | ui-extractor (Rust CLI) | MIT       | Image screenshot                   | JSON UI elements (contours, texte, icônes) | Extr. UI tree par contours CV + OCR via local-infer-core. Exécutable Windows disponible. |
|                                  | Tesseract / pytesseract              | Apache   | Image (screen)                     | Texte brut (OCR)                 | Standard de facto pour OCR (Lang Python). Stage secondaire facile. |
|                                  | EasyOCR (pip)                        | Apache   | Image                              | Texte brut                       | Alternative OCR rapide sur CPU/GPU.                        |
|                                  | LAVIS / BLIP (Python)               | MIT       | Image                              | Caption (phrase)                | Bibliothèque vision/Langage (Salesforce) pour générer des captions génériques d’écrans. |
| **Reconnaissance d’actions**     | Screex (Python CLI)       | MIT       | Vidéo                              | État 1,2,… (transitions textuelles) | Sort: `index.json` (états d’écran, texte OCR, thumb + full images). Très adapté aux screencasts. |
|                                  | Video-to-SOP (Python/Streamlit) | MIT (?) | Vidéo                              | DOCX étapes détaillées          | Pipeline complet (Frame extract, OCR, segm., GPT). Documenté dans README. |
|                                  | Stepthrough (JS/Python)             | MIT       | Vidéo + annotations                | Guide étape par étape           | Interface web (ébauche, 1 contribut.). Moins mature.       |
|                                  | StepSnap (Tauri/JS)      | Apache-2.0| Sessions enregistrement live       | Guide éditable (PDF/DOCX)       | Capture en direct (clics, clavier) et éditeur d’étapes. Big client lourd. |
| **Capture instrumentée**         | OpenAdapt Capture (Python)  | MIT       | Session utilisateur (live)         | Base d’actions (clic, touche, UI) | Enregistre souris+clavier, screenshots, a11y. API Python fournissant DB actions+images. |
|                                  | Captr_Windows/Mac (Python/OBS) | MIT       | Session utilisateur (live)        | Vidéo + logs (DOM, a11y, input) | Enregistre via OBS + extension Chrome. Collecte DOM/a11y/inputs. GitHub actif (Fenêtres). |
|                                  | Computer-Use-Recorder (Python) | *?*       | Session utilisateur (Windows)      | Vidéo + a11y trees + interactions | Conçu pour recherche AT. Montre usage clavier/souris + arbre d’accessibilité. Complexe (OBS, Chrome). |
|                                  | brogcursor (Python)                 | MIT       | Session utilisateur               | Enregistre clics/saisies (replay) | Outil de journalisation et reproduction d’actions souris/clavier. |
| **Segmentation et workflow**     | PM4Py (Python)                      | Apache   | Log d’événements (JSON)            | Process model, séquences fréquentes | Librairie de “process mining” pour extraire des tâches/règles dans un log. |
|                                  | PréfixSpan (Python)                 | MIT       | Séquence d’actions textuelles      | Séquences fréquentes            | Extraction de sous-séquences fréquentes (apprêtée à reconnaître motifs répétitifs). |
|                                  | (Aucun outil dédié spécifique GUI)  | –         | –                                 | –                               | On s’en remettrait à des approches génériques (clustering, LCS, séquenceSQL). |

Ce tableau compare les candidats par étape. On voit, par exemple, que **Keyframes-Extractor** couvre l’étape d’extraction de frames, tandis qu’**OmniParser** et **ui-extractor** s’occupent de l’analyse structurelle des captures d’écran. *Screex* ingère la vidéo complète et produit un index de scènes avec texte. Les enregistreurs instrumentés (OpenAdapt, Captr, etc.) élaguent pratiquement tout travail de vision par l’obtention directe d’événements et screenshots. 

# Architectures recommandées

## Approche existante (MP4)  
1. **Extraction de frames.** Utiliser par exemple *Keyframes-Extractor* pour générer des captures d’écran stables horodatées. (Si non, PySceneDetect peut extraire des scènes similaires.)  
2. **Descriptions d’écran.** Sur chaque capture stable : lancer un OCR (Tesseract/EasyOCR) pour extraire les textes visibles. En option, passer l’image à un captionneur (BLIP/LAVIS) ou un parseur UI (OmniParser ou ui-extractor) pour obtenir la structure de l’interface. Ainsi on obtient, pour chaque image, un résumé textuel ou JSON.  
3. **Inférence d’actions/transitions.** Comparer images consécutives : on peut réutiliser *Screex* pour détecter automatiquement les changements de texte et générer un « transcript » séquencé. Sinon, faire du diff image (OpenCV) ou texte (LLM léger) pour formuler « clique sur X », « saisie dans Y ».  
4. **Regroupement en étapes.** Grouper les états/actions contigus ou similaires : e.g. fusionner des transitions répétées en un même segment, détecter motifs fréquents (par exemple en remplaçant les données variables par des placeholders et en comparant). L’outil *PM4Py* peut aider à visualiser des modèles de processus (à condition de formater le log d’actions).  
5. **Flux de données.** Dans cette architecture, les données passent : vidéo MP4 → images (Keyframes-Extractor) → textes JSON (OCR/parseur) → séquences/actions (Screex ou code maison) → structure finale (JSON).  
6. **Points critiques.** Le *maillon faible* est souvent l’OCR sur écrans complexes (icônes ou polices fines) et la déduction des actions. Sans LLM avancé, il faudra s’appuyer sur règles simples (exemple : toute apparition soudaine d’un dialogue = « fenêtre ouverte »). Le détection de paresse (ex. petits scrolls) peut produire trop d’états similaires. 

## Approche instrumentée (enregistrement live)  
1. **Enregistrement.** Utiliser *OpenAdapt-Capture* (ou Captr) pour que l’utilisateur effectue la tâche normale. L’outil enregistre souris/clavier, captures d’écran et même l’arbre d’accessibilité quand disponible.  
2. **Accès aux données.** À la fin, on obtient une base de données d’actions : chaque entrée comprend timestamp, type (clic, frappe, etc.), coordonnée, texte saisi, et l’image d’écran associée. On a donc déjà liaisons explicites « action + écran ».  
3. **Extraction du workflow.** Les actions étant explicites, il suffit de les formater en étapes. Par ex. chaque clic de menu déclenche un nouvel “état de workflow”. On peut enrichir par OCR si nécessaire (par ex. l’arbre a11y fournit déjà le label du bouton cliqué). Les transitions sont instantanées puisque capturées.  
4. **Flux de données.** La capture génère directement un format structuré : pas de vidéo intermédiaire. Ensuite on exporte depuis Python en JSON personnalisé.  
5. **Bottlenecks.** Le principal enjeu est la configuration de l’enregistreur (permissions, OBS requis sous Windows, etc.). Le temps d’installation/essai est plus long que l’approche MP4. En revanche, on gagne en fiabilité sur la reconnaissance d’actions et l’exhaustivité (aucun clic manqué, pas de vision par ordinateur). 

# Pile recommandée (prototype 10h)

Pour un hackathon de 10 h, on privilégie la simplicité d’assemblage :

1. **Screex (Python)** – *pip install screex*. Ce seul outil fournit une segmentation d’écran avec OCR. Depuis la vidéo MP4, lancez `screex index enregistrement.mp4`; vous obtenez un dossier `.screex/` avec un `index.json` contenant, pour chaque état, la capture (full-res) et le texte OCR. Vous gagnez l’essentiel des étapes 1–3 en une commande.  
2. **OCR supplémentaire.** Bien que Screex intègre son OCR, on peut ajouter *pytesseract* au cas où. Par exemple, extraire le texte brut d’images ou compléter des zones floues pour la compréhension.  
3. **LLM Multimodal (Mistral)** – Après extraction des états avec Screex, on alimente Mistral avec l’index JSON pour générer les descriptions d’actions ou le regroupement en étapes. Par exemple, on peut demander à Mistral de lire chaque entrée (texte + mini-capture) et produire une phrase d’action (“l’utilisateur a ouvert le menu X puis a saisi Y”). Mistral est essentiel pour interpréter les changements observés.  
4. **Outils optionnels :** Pour des améliorations minimes, on peut pip-installer *opencv-python* pour recadrer/coller images, ou *easyocr* si Tesseract manque. En cas de temps, intégrer **Keyframes-Extractor** avant Screex pour éviter des états sur-abondants (il y a  file d’entrée par frames pour `screex`, mais on peut aussi recourir aux clés Selenium).  
5. **Intégration rapide :** Démarrer par `pip install screex pytesseract matplotlib` (par ex.), puis écrire un script minimal en Python qui : (a) exécute Screex et charge `index.json`, (b) formate la sortie JSON attendue (voir schéma ci-dessous), (c) optionnellement appelle Mistral via API sur chaque segment. 

Dans cette pile minimale, **Screex** est le pivot (“entrée : vidéo; sortie : segments OCR”); tout le reste (parsing JSON, appel LLM) peut être un petit script Python. Cela évite d’installer des dépendances lourdes (Keras, PyTorch) et offre un point d’entrée clair (`screex index ...`). Les améliorations facultatives (comme OmniParser) sont à réserver si le temps le permet.

# Schéma de données intermédiaire proposé (JSON)

Un format JSON structuré pourrait être : 

```json
{
  "recording_id": "session123",
  "duration": 312.4,
  "states": [
    {
      "state_id": 1,
      "start_time": 0.0,
      "end_time": 5.2,
      "screenshot": "state1.png",
      "description": "Écran de connexion – champs Email et Mot de passe",
      "observations": ["Champs: Email", "Champs: Mot de passe"],
      "actions": [],
      "confidence": 0.92
    },
    {
      "state_id": 2,
      "start_time": 5.2,
      "end_time": 6.0,
      "screenshot": "state2.png",
      "description": "Tableau de bord – menu principal visible",
      "observations": ["Texte: Dashboard", "Bouton: Accueil"],
      "actions": ["Clic sur bouton Login"],
      "confidence": 0.88
    }
    // ...
  ],
  "steps": [
    {
      "step_id": 1,
      "states": [1,2],
      "time_range": [0.0, 6.0],
      "actions": ["Connexion utilisateur"],
      "evidence": ["state1.png", "state2.png"],
      "uncertainty": 0.1
    }
    // ...
  ]
}
```

Chaque **state** correspond à un état d’écran capturé (avec horodatages, fichier image et texte/éléments visibles). On y note aussi la ou les **actions observées** (ici “clic sur Login”) et un champ `confidence` pour indiquer la fiabilité (p. ex. qualité OCR ou du modèle). Les **steps** regroupent plusieurs états en une étape métier cohérente, listent les actions clés exécutées, et référencent les images d’évidence. On peut y ajouter des champs `confidence` ou `uncertainty` pour refléter le doute (ex. “actions inventées ?”). Ce schéma est facilement enrichissable (par ex. phases, ref. DOM/accessibilité, etc.) et sert de pont entre l’analyse et la modélisation Mistral.

# Expérience de validation minimale

Pour valider rapidement la chaîne sur un court screencast (2–3 minutes) :

1. **Test de détection de transitions :** Vérifier que chaque changement important (nouveau menu, nouveau document ouvert, etc.) apparaît dans `states`. Si une action visible (ex. ouverture de fenêtre) ne crée pas de nouvelle entrée `state`, on doit affiner le seuil (ex. changer `--change-threshold` de Screex ou diff d’image).  
2. **Test d’OCR :** Extraire le texte à la main sur quelques images clés et comparer au texte produit. Repérer les champs partiellement reconnus (par exemple chaînes tronquées). Ajuster la prépro ou ajouter EasyOCR au besoin.  
3. **Test d’actions hallucinéess :** S’assurer qu’aucune action « n’est inventée » par rapport à la vidéo. Par ex. dans le JSON final, chaque `action` listée doit correspondre à un événement réel (p. ex. vue dans l’écran ou dans les logs). Écouter le screencast (audio) au besoin pour confirmer les actions.  
4. **Test de répétition non éliminée :** Si la vidéo revient plusieurs fois sur le même écran (par ex. menu principal après sous-menu), vérifier que ces états distincts sont tous présents (par ex. différents timestamps, même `description`). Ne pas les filtrer comme de simples duplicatas.  
5. **Réconciliation vers le brut :** Toujours relier chaque étape ou action au segment vidéo original (les timestamps ou images dans le JSON permettent cette traçabilité). L’indice est qu’on peut rejouer exactement sur la vidéo les moments notés dans le JSON. Si un segment critique du screencast n’apparaît pas, le pipeline a manqué une transition (à corriger).

Cette validation couvre les échecs typiques : seuils mal calibrés, OCR manqué, créativité du modèle. Un *script de vérification* peut scanner le JSON final pour s’assurer que 1) le nombre de `states` est raisonnable, 2) tous contiennent du texte (pas d’entrée vide), 3) chaque `screenshot` référencé existe.

# Solutions de repli (fallbacks)

- **Difficultés d’installation (GPU, dépendances) :** Si OmniParser ou Deki sont trop lourds, on peut se replier sur des détecteurs basiques (p. ex. *ui-extractor* avec Tesseract, ou même `opencv` + heuristiques pour repérer des boutons). Pour le captioning d’écran, utiliser BLIP/Torch ou simplement renvoyer le texte brut OCR.  
- **Clé API/Licence externe non disponible :** Video-to-SOP fait grand usage de l’API OpenAI; sans clé on reste sur un mode “diagnostic OCR” (texte brut). En prototypage, on peut ignorer la génération de phrases et afficher “Texte OCR: …” ou “État de l’écran: …” au lieu d’une phrase polie.  
- **Pas de machine GPU :** Des modèles YOLO (OmniParser) sont AGPL et lourds ; en fallback on peut détecter des régions claires (boutons, champs) par détection de contours (ui-extractor) ou renoncer à la détection d’icône. Screex et Keyframes-Extractor fonctionnent sur CPU.  
- **Enregistreur complexe introuvable :** Si on ne peut pas installer OpenAdapt/Captr (droits admin, etc.), on retombe sur l’upload vidéo classique. On perd les données d’accessibilité, mais on garde l’avantage de fonctionner avec n’importe quel enregistrement préexistant.  
- **Traitement d’étape manuel :** En dernière extrémité, si aucun pipeline automatique n’est fiable, on peut extraire manuellement  le passage vidéo (par ex. à l’aide d’OBS) et écrire à la main quelques notes de étapes et captures pour la démo. Cela évite un crash complet et permet de montrer un résultat (même basique).  

Les composants suggérés ont été vérifiés à partir des sources publiques (GitHub, PyPI) et testés via code ou documentation. La date de cette recherche est le 21 novembre 2026, reflétant l’état de l’art le plus récent. 

**Sources de données :** documentation et code publiés sur GitHub/PyPI pour chacun des outils cités, ainsi que la littérature associée (y compris arXiv cité dans OmniParser).