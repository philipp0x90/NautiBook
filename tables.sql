-- Maritime Database Schema for SQLite
-- Translated from FileMaker Pro structure

-- ============================================
-- Table: ta_Navires (Vessels/Ships)
-- ============================================
CREATE TABLE ta_Navires (
    _ID_navire INTEGER PRIMARY KEY AUTOINCREMENT,
    navire_nom TEXT,
    _navire_Statique_UUID TEXT UNIQUE,
    navire_modele TEXT,
    
    -- Capacity
    capacite_eau REAL,
    capacite_gasoil REAL,
    consommation_réputée REAL,
    gasoil_autonomie_théorique REAL,
    
    -- Insurance
    assurance_date_effet DATE,
    assurance_date_fin DATE,
    assurance_numero_pol TEXT,
    
    -- Dimensions
    dimensions_deplacement_lest REAL,
    dimensions_hauteur_franc_bord REAL,
    dimensions_hauteur_mat REAL,
    dimensions_largeur REAL,
    dimensions_longueur_coque REAL,
    dimensions_longueur_flottaison REAL,
    dimensions_longueur_ht REAL,
    dimensions_poids_lest REAL,
    dimensions_surface REAL,
    dimensions_surface_gd_voile REAL,
    dimensions_surface_genois REAL,
    dimensions_surface_spi REAL,
    dimensions_TA REAL,
    dimensions_TA_bateau REAL,
    dimensions_TA_ss_mat REAL,
    dimensions_TE REAL,
    
    -- Engine
    moteur_date_horametre_init DATE,
    moteur_horametre_init REAL,
    moteur_marque TEXT,
    moteur_modele TEXT,
    moteur_num_serie TEXT,
    moteur_puissance REAL,
    
    -- Vessel Information
    navire_CallSign TEXT,
    navire_constructeur TEXT,
    navire_construction_annee INTEGER,
    navire_immatriculation TEXT,
    navire_infos_diverses TEXT,
    navire_materiau TEXT,
    navire_MMSI TEXT,
    navire_numero_de_serie TEXT,
    navire_pavillon TEXT,
    navire_port_d_attache TEXT,
    navire_registre_date DATE,
    navire_registre_numero TEXT,
    navire_registre_validite DATE
);

-- ============================================
-- Table: ta_Croisière (Cruises)
-- ============================================
CREATE TABLE ta_Croisière (
    _ID_Croisière INTEGER PRIMARY KEY AUTOINCREMENT,
    _Croisière_UUID TEXT UNIQUE,
    ext_ID_navire INTEGER,
    ext_nom_navire TEXT,
    ext_RO_Numero_Route INTEGER,
    ext_ToDo_Etat TEXT,
    ext_ID_LigneGASOIL INTEGER,
    
    -- Cruise dates
    Croisière_date_debut DATE,
    Croisière_date_fin DATE,
    Durée REAL,
    
    -- Odometer and engine hours
    ODO_début_croisière REAL,
    ODO_fin_croisière REAL,
    Horamètre_début_croisière REAL,
    Horamètre_fin_croisière REAL,
    heures_moteur REAL,
    milles_parcourus REAL,
    
    -- Location
    Croisière_Origine TEXT,
    Croisière_Destination TEXT,
    
    -- Fuel
    GOdébutCroisière REAL,
    GOfinCroisière REAL,
    GOajouté REAL,
    
    -- Skipper
    Skipper TEXT,
    
    FOREIGN KEY (ext_ID_navire) REFERENCES ta_Navires(_ID_navire)
);

-- ============================================
-- Table: ta_Routes (Routes)
-- ============================================
CREATE TABLE ta_Routes (
    _ID_Route INTEGER PRIMARY KEY AUTOINCREMENT,
    _RouteUUID TEXT UNIQUE,
    ext_ID_Croisière INTEGER,
    
    -- Route timing
    RO_Moment_Depart DATETIME,
    RO_Moment_Arrivee DATETIME,
    RO_duree REAL,
    
    -- Route details
    RO_Origine TEXT,
    RO_Destination TEXT,
    RO_total_trip REAL,
    RO_vitesse_moyenne REAL,
    
    -- Engine hours
    RO_HoramètreDépartRoute REAL,
    RO_HoramètreArrivéeRoute REAL,
    RO_HoramètreTotalRoute REAL,
    
    -- Fuel consumption
    RO_ConsommationEstiméeRoute REAL,
    RO_ConsommationCumulée REAL,
    RO_EstimationGasoilRestant REAL,
    
    -- Additional fields
    NM_So_far REAL,
    Journal TEXT,
    ServerAddress TEXT,
    MRN_IMO TEXT,
    AllAvailableData TEXT,
    AllAvailableData_Formated TEXT,
    LastData TEXT,
    
    FOREIGN KEY (ext_ID_Croisière) REFERENCES ta_Croisière(_ID_Croisière)
);

-- ============================================
-- Table: ta_Lignes (Log Lines)
-- ============================================
CREATE TABLE ta_Lignes (
    _ID_Ligne INTEGER PRIMARY KEY AUTOINCREMENT,
    _Ligne_UUID TEXT UNIQUE,
    ext_ID_Route INTEGER,
    ext_ToDo_Statut_item TEXT,
    ext_AfficherEau BOOLEAN,
    ext_AfficherGasoil BOOLEAN,
    
    -- Timestamp
    Date DATE,
    Horodatage_de_création DATETIME,
    Heure TIME,
    
    -- Weather
    VentVit REAL,
    VentDir TEXT,
    Mer TEXT,
    Visi TEXT,
    Température REAL,
    Température_de_leau REAL,
    
    -- Navigation
    Speed REAL,
    Cap REAL,
    COG REAL,
    Voiles TEXT,
    Allure TEXT,
    Odo REAL,
    Trip REAL,
    Sonde REAL,
    
    -- Position
    Lat REAL,
    Long REAL,
    x_Lat_gps REAL,
    x_Long_gps REAL,
    LI_Pos_Visu TEXT,
    LI_Trip_jour REAL,
    Menu_de_navigation TEXT,
    
    FOREIGN KEY (ext_ID_Route) REFERENCES ta_Routes(_ID_Route)
);

-- ============================================
-- Table: ta_Escales (Stopovers)
-- ============================================
CREATE TABLE ta_Escales (
    ID_Escale INTEGER PRIMARY KEY AUTOINCREMENT,
    _EscaleUUID TEXT UNIQUE,
    ext_ID_Route INTEGER,
    ext_ID_Croisière INTEGER,
    
    -- Location
    Localité TEXT,
    Marina TEXT,
    Type TEXT,
    
    -- Dates
    Date_arrivée DATE,
    Date_départ DATE,
    Nuitées INTEGER,
    
    -- Cost
    Prix REAL,
    Total_escales REAL,
    
    FOREIGN KEY (ext_ID_Route) REFERENCES ta_Routes(_ID_Route),
    FOREIGN KEY (ext_ID_Croisière) REFERENCES ta_Croisière(_ID_Croisière)
);

-- ============================================
-- Table: ta_Gasoil (Fuel/Diesel)
-- ============================================
CREATE TABLE ta_Gasoil (
    _ID_LigneGASOIL INTEGER PRIMARY KEY AUTOINCREMENT,
    _Gasoil_UUID TEXT UNIQUE,
    ext_ID_Croisière INTEGER,
    ext_ID_Navire INTEGER,
    ext_navire_nom TEXT,
    
    -- Refueling
    Date_nouveau_plein DATE,
    Horamètre REAL,
    Qté REAL,
    Prix_L REAL,
    MontantPlein REAL,
    
    -- Consumption
    Heures REAL,
    Moyenne_de_consommation REAL,
    Conso REAL,
    TotalGasoil REAL,
    TotalGasoil_Litres REAL,
    Horamètre_dernier_plein REAL,
    
    FOREIGN KEY (ext_ID_Croisière) REFERENCES ta_Croisière(_ID_Croisière),
    FOREIGN KEY (ext_ID_Navire) REFERENCES ta_Navires(_ID_navire)
);

-- ============================================
-- Table: ta_Eau (Water)
-- ============================================
CREATE TABLE ta_Eau (
    ID_LigneEAU INTEGER PRIMARY KEY AUTOINCREMENT,
    _EauUUID TEXT UNIQUE,
    ext_ID_Croisière INTEGER,
    
    -- Readings
    Date_Relevé_Eau DATE,
    Debimetre REAL,
    Jours INTEGER,
    Volume REAL,
    AfficherEau BOOLEAN,
    
    -- Consumption
    Consommation_Eau REAL,
    TotalVolume REAL,
    TotalJours INTEGER,
    MoyenneConso REAL,
    QuantitéPrésumée_1 REAL,
    Dessalator REAL,
    QuantitéPrésumée_totale REAL,
    
    FOREIGN KEY (ext_ID_Croisière) REFERENCES ta_Croisière(_ID_Croisière)
);

-- ============================================
-- Table: ta_Equipiers (Crew Members)
-- ============================================
CREATE TABLE ta_Equipiers (
    _ID_Equipier INTEGER PRIMARY KEY AUTOINCREMENT,
    _Equipier_UUID TEXT UNIQUE,
    ext_ID_Equipage INTEGER,
    
    -- Name
    Nom TEXT,
    Prénom TEXT,
    Nom_complet TEXT,
    Initiales TEXT,
    
    -- Address
    Rue_et_numéro TEXT,
    Localité TEXT,
    CP TEXT,
    Pays TEXT,
    Nationalité TEXT,
    
    -- Birth information
    Date_de_naissance DATE,
    Lieu_de_naissance TEXT,
    Age INTEGER,
    
    -- Identification
    ID_Type TEXT,
    ID_Nr TEXT,
    
    -- Contact
    email TEXT,
    Téléphone TEXT,
    
    -- Other
    Photo BLOB,
    NN TEXT,
    ListeEq TEXT
);

-- ============================================
-- Table: ta_Equipages (Crew Assignments)
-- ============================================
CREATE TABLE ta_Equipages (
    _ID_Equipage INTEGER PRIMARY KEY AUTOINCREMENT,
    _Equipage_UUID TEXT UNIQUE,
    ext_ID_Croisière INTEGER,
    ext_ID_Equipier INTEGER,
    
    -- Boarding dates
    Embarquement DATE,
    Débarquement DATE,
    
    FOREIGN KEY (ext_ID_Croisière) REFERENCES ta_Croisière(_ID_Croisière),
    FOREIGN KEY (ext_ID_Equipier) REFERENCES ta_Equipiers(_ID_Equipier)
);

-- ============================================
-- Table: ta_ToDo (To-Do Items)
-- ============================================
CREATE TABLE ta_ToDo (
    _ID_ToDO INTEGER PRIMARY KEY AUTOINCREMENT,
    _ToDo_UUID TEXT UNIQUE,
    ext_ID_Navire INTEGER,
    
    -- Timestamps
    Horodatage_de_création DATETIME,
    
    -- Task details
    Tâche TEXT,
    Echéance DATE,
    Statut TEXT,
    Date_réalisation DATE,
    Liste TEXT,
    Urgent BOOLEAN,
    
    FOREIGN KEY (ext_ID_Navire) REFERENCES ta_Navires(_ID_navire)
);

-- ============================================
-- Table: ta_Checklist (Checklist Items)
-- ============================================
CREATE TABLE ta_Checklist (
    Checklist_ID INTEGER PRIMARY KEY AUTOINCREMENT,
    Clé_primaire TEXT UNIQUE,
    ext_ID_Navire INTEGER,
    
    -- Timestamps
    Horodatage_de_création DATETIME,
    Horodatage_de_modification DATETIME,
    
    -- Checklist item
    Checklist_Item TEXT,
    Checklist_Item_Status TEXT,
    
    FOREIGN KEY (ext_ID_Navire) REFERENCES ta_Navires(_ID_navire)
);

-- ============================================
-- Indexes for better performance
-- ============================================

-- Navires indexes
CREATE INDEX idx_navires_nom ON ta_Navires(navire_nom);
CREATE INDEX idx_navires_uuid ON ta_Navires(_navire_Statique_UUID);

-- Croisière indexes
CREATE INDEX idx_croisiere_navire ON ta_Croisière(ext_ID_navire);
CREATE INDEX idx_croisiere_dates ON ta_Croisière(Croisière_date_debut, Croisière_date_fin);

-- Routes indexes
CREATE INDEX idx_routes_croisiere ON ta_Routes(ext_ID_Croisière);
CREATE INDEX idx_routes_dates ON ta_Routes(RO_Moment_Depart, RO_Moment_Arrivee);

-- Lignes indexes
CREATE INDEX idx_lignes_route ON ta_Lignes(ext_ID_Route);
CREATE INDEX idx_lignes_date ON ta_Lignes(Date);

-- Escales indexes
CREATE INDEX idx_escales_route ON ta_Escales(ext_ID_Route);
CREATE INDEX idx_escales_croisiere ON ta_Escales(ext_ID_Croisière);

-- Gasoil indexes
CREATE INDEX idx_gasoil_croisiere ON ta_Gasoil(ext_ID_Croisière);
CREATE INDEX idx_gasoil_navire ON ta_Gasoil(ext_ID_Navire);
CREATE INDEX idx_gasoil_date ON ta_Gasoil(Date_nouveau_plein);

-- Eau indexes
CREATE INDEX idx_eau_croisiere ON ta_Eau(ext_ID_Croisière);

-- Equipiers indexes
CREATE INDEX idx_equipiers_nom ON ta_Equipiers(Nom, Prénom);

-- Equipages indexes
CREATE INDEX idx_equipages_croisiere ON ta_Equipages(ext_ID_Croisière);
CREATE INDEX idx_equipages_equipier ON ta_Equipages(ext_ID_Equipier);

-- ToDo indexes
CREATE INDEX idx_todo_navire ON ta_ToDo(ext_ID_Navire);
CREATE INDEX idx_todo_echeance ON ta_ToDo(Echéance);
CREATE INDEX idx_todo_statut ON ta_ToDo(Statut);

-- Checklist indexes
CREATE INDEX idx_checklist_navire ON ta_Checklist(ext_ID_Navire);
