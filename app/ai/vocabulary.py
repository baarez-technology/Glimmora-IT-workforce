"""Skill and technology vocabulary used by the deterministic parser.

This is the knowledge that lets the platform work with no LLM configured. It is
narrow on purpose — the SOW scopes V1 to enterprise IT — and it doubles as the
seed for the `skills` master so that parsed skills land on canonical names
rather than whatever spelling a client happened to use.
"""

from __future__ import annotations

#: canonical name -> (category, aliases)
SKILL_VOCABULARY: dict[str, tuple[str, list[str]]] = {
    # --- SAP ------------------------------------------------------------
    "SAP S/4HANA": ("SAP", ["s4hana", "s/4 hana", "s4 hana", "s/4hana", "sap s4"]),
    "SAP FICO": ("SAP", ["fico", "sap fi/co", "sap finance and controlling", "sap fi co"]),
    "SAP MM": ("SAP", ["materials management", "sap materials"]),
    "SAP SD": ("SAP", ["sales and distribution"]),
    "SAP ABAP": ("SAP", ["abap", "abap oo"]),
    "SAP BASIS": ("SAP", ["basis"]),
    "SAP SuccessFactors": ("SAP", ["successfactors", "success factors"]),
    # --- Oracle ---------------------------------------------------------
    "Oracle EBS": ("Oracle", ["e business suite", "e-business suite", "ebs"]),
    "Oracle Fusion": ("Oracle", ["fusion cloud", "oracle cloud erp"]),
    "Oracle Database": ("Oracle", ["oracle dba", "oracle db", "oracle 19c"]),
    "PL/SQL": ("Oracle", ["plsql", "pl sql"]),
    # --- Microsoft ------------------------------------------------------
    "Microsoft Dynamics 365": ("Microsoft", ["dynamics 365", "d365", "dynamics"]),
    "SharePoint": ("Microsoft", ["share point"]),
    "Power BI": ("Microsoft", ["powerbi", "power-bi"]),
    "Power Platform": ("Microsoft", ["power apps", "powerapps", "power automate"]),
    ".NET": ("Microsoft", ["dotnet", "dot net", "asp.net", "c#", "csharp"]),
    "Active Directory": ("Microsoft", ["ad", "azure ad", "entra id"]),
    # --- Cloud ----------------------------------------------------------
    "AWS": ("Cloud", ["amazon web services"]),
    "Azure": ("Cloud", ["microsoft azure"]),
    "Google Cloud": ("Cloud", ["gcp", "google cloud platform"]),
    "Kubernetes": ("Cloud", ["k8s", "eks", "aks", "openshift"]),
    "Docker": ("Cloud", ["containers", "containerisation", "containerization"]),
    "Terraform": ("Cloud", ["iac", "infrastructure as code"]),
    "CI/CD": ("Cloud", ["cicd", "ci cd", "jenkins", "gitlab ci", "devops pipeline"]),
    # --- Data & AI ------------------------------------------------------
    "Data Engineering": ("AI / Data", ["etl", "elt", "data pipelines", "data pipeline"]),
    "Machine Learning": ("AI / Data", ["ml", "deep learning", "mlops"]),
    "Databricks": ("AI / Data", ["spark", "pyspark", "apache spark"]),
    "Snowflake": ("AI / Data", []),
    "Tableau": ("AI / Data", []),
    # --- Cybersecurity --------------------------------------------------
    "SIEM": ("Cybersecurity", ["splunk", "qradar", "sentinel"]),
    "SOC Operations": (
        "Cybersecurity",
        ["soc", "security operations centre", "security operations center"],
    ),
    "Identity & Access Management": ("Cybersecurity", ["iam", "sailpoint", "okta", "cyberark"]),
    "Penetration Testing": ("Cybersecurity", ["pen test", "pentest", "ethical hacking", "vapt"]),
    "ISO 27001": ("Cybersecurity", ["iso27001", "isms"]),
    "OT / SCADA Security": ("Cybersecurity", ["scada", "ics security", "ot security"]),
    # --- Application development ----------------------------------------
    "Java": ("Java / Application Development", ["core java", "j2ee", "jee"]),
    "Spring Boot": ("Java / Application Development", ["spring", "springboot"]),
    "Python": ("Java / Application Development", ["python3"]),
    "JavaScript": ("Java / Application Development", ["js", "es6"]),
    "TypeScript": ("Java / Application Development", ["ts"]),
    "React": ("Java / Application Development", ["react.js", "reactjs"]),
    "Angular": ("Java / Application Development", ["angularjs", "angular 2+"]),
    "Node.js": ("Java / Application Development", ["nodejs", "node"]),
    "REST APIs": ("Java / Application Development", ["rest api", "restful", "api development"]),
    "Microservices": ("Java / Application Development", ["micro services"]),
    # --- QA -------------------------------------------------------------
    "Test Automation": ("QA / Testing", ["automation testing", "automated testing"]),
    "Selenium": ("QA / Testing", []),
    "Performance Testing": ("QA / Testing", ["load testing", "jmeter", "loadrunner"]),
    "Manual Testing": ("QA / Testing", ["functional testing"]),
    # --- Infrastructure -------------------------------------------------
    "Networking": ("IT Infrastructure", ["cisco", "ccna", "ccnp", "lan wan", "sd-wan"]),
    "Linux": ("IT Infrastructure", ["rhel", "red hat", "ubuntu server"]),
    "Windows Server": ("IT Infrastructure", ["windows administration"]),
    "VMware": ("IT Infrastructure", ["vsphere", "esxi", "virtualisation", "virtualization"]),
    "Storage & Backup": ("IT Infrastructure", ["san", "nas", "veeam", "netbackup"]),
    "ITIL": ("IT Infrastructure", ["service management", "itsm", "servicenow"]),
    # --- Delivery -------------------------------------------------------
    "Project Management": ("Delivery", ["pmp", "prince2", "project manager"]),
    "Agile / Scrum": ("Delivery", ["scrum", "agile", "safe", "kanban"]),
    "Business Analysis": ("Delivery", ["business analyst", "ba"]),
}

#: Technology family for a canonical skill, used to derive the requirement stack.
SKILL_TO_TECHNOLOGY: dict[str, str] = {
    "SAP": "SAP",
    "Oracle": "Oracle",
    "Microsoft": "Microsoft",
    "Cloud": "Cloud",
    "AI / Data": "AI / Data",
    "Cybersecurity": "Cybersecurity",
    "Java / Application Development": "Java / Application Development",
    "QA / Testing": "QA / Testing",
    "IT Infrastructure": "IT Infrastructure",
}

#: Role families recognised in a title, most specific first.
ROLE_KEYWORDS: list[tuple[str, list[str]]] = [
    ("SAP Consultant", ["sap consultant", "sap functional", "sap fico", "sap mm", "sap sd"]),
    ("SAP Technical Consultant", ["abap", "sap technical", "sap basis"]),
    ("Oracle Consultant", ["oracle consultant", "oracle functional", "ebs consultant"]),
    ("Database Administrator", ["dba", "database administrator"]),
    ("Cloud Engineer", ["cloud engineer", "cloud architect", "devops engineer", "sre"]),
    ("Data Engineer", ["data engineer", "etl developer", "bi developer"]),
    ("Data Scientist", ["data scientist", "machine learning engineer", "ml engineer"]),
    ("Security Engineer", ["security engineer", "security analyst", "soc analyst"]),
    ("Security Architect", ["security architect", "ciso", "security consultant"]),
    ("Java Developer", ["java developer", "java engineer", "backend developer"]),
    ("Frontend Developer", ["frontend developer", "front-end developer", "ui developer"]),
    ("Full Stack Developer", ["full stack", "fullstack"]),
    ("QA Engineer", ["qa engineer", "test engineer", "qa analyst", "test analyst"]),
    ("Network Engineer", ["network engineer", "network administrator"]),
    (
        "System Administrator",
        ["system administrator", "systems engineer", "infrastructure engineer"],
    ),
    ("Solution Architect", ["solution architect", "enterprise architect", "technical architect"]),
    (
        "Project Manager",
        ["project manager", "programme manager", "program manager", "delivery manager"],
    ),
    ("Business Analyst", ["business analyst", "functional analyst"]),
    ("Scrum Master", ["scrum master", "agile coach"]),
    (
        "Support Engineer",
        ["support engineer", "service desk", "helpdesk", "l2 support", "l3 support"],
    ),
]

#: City -> ISO country, biased to the Gulf markets the SOW targets.
LOCATION_COUNTRIES: dict[str, str] = {
    "doha": "QA",
    "qatar": "QA",
    "lusail": "QA",
    "al wakrah": "QA",
    "ras laffan": "QA",
    "dubai": "AE",
    "abu dhabi": "AE",
    "sharjah": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "riyadh": "SA",
    "jeddah": "SA",
    "dammam": "SA",
    "saudi arabia": "SA",
    "ksa": "SA",
    "manama": "BH",
    "bahrain": "BH",
    "kuwait": "KW",
    "kuwait city": "KW",
    "muscat": "OM",
    "oman": "OM",
    "bangalore": "IN",
    "bengaluru": "IN",
    "hyderabad": "IN",
    "chennai": "IN",
    "pune": "IN",
    "mumbai": "IN",
    "india": "IN",
    "london": "GB",
    "united kingdom": "GB",
}

#: Currency symbols and codes seen in Gulf and offshore rate cards.
CURRENCY_TOKENS: dict[str, str] = {
    "qar": "QAR",
    "qr": "QAR",
    "riyal": "QAR",
    "riyals": "QAR",
    "aed": "AED",
    "dhs": "AED",
    "dirham": "AED",
    "dirhams": "AED",
    "sar": "SAR",
    "usd": "USD",
    "$": "USD",
    "us$": "USD",
    "eur": "EUR",
    "€": "EUR",
    "gbp": "GBP",
    "£": "GBP",
    "inr": "INR",
    "₹": "INR",
    "rs": "INR",
}


#: Entries in LOCATION_COUNTRIES that name a country rather than a city.
#: A CV saying "Doha, Qatar" must yield city=Doha, not city=Qatar.
COUNTRY_TOKENS: frozenset[str] = frozenset(
    {
        "qatar",
        "uae",
        "united arab emirates",
        "saudi arabia",
        "ksa",
        "bahrain",
        "kuwait",
        "oman",
        "india",
        "united kingdom",
    }
)


def build_alias_index() -> dict[str, str]:
    """alias (normalised) -> canonical skill name.

    Built once at import by the parser. Longer aliases are matched first so that
    "sap fico" wins over a bare "sap".
    """
    from app.models.skills import normalize_skill

    index: dict[str, str] = {}
    for canonical, (_category, aliases) in SKILL_VOCABULARY.items():
        index[normalize_skill(canonical)] = canonical
        for alias in aliases:
            index[normalize_skill(alias)] = canonical
    return index


__all__ = [
    "COUNTRY_TOKENS",
    "CURRENCY_TOKENS",
    "LOCATION_COUNTRIES",
    "ROLE_KEYWORDS",
    "SKILL_TO_TECHNOLOGY",
    "SKILL_VOCABULARY",
    "build_alias_index",
]
