# Hypertension Cohort Identification

- **Author:** Htun Teza
- **Date:** 15 January 2024

> [!IMPORTANT]  
> Please be aware that the information provided here is current as of the date of this documentation. However, it is subject to potential changes or updates in the future.


## Contents
- [ETL procedure](#etl-procedure)
    - [Instance 1](#instance-1)
- [Loosely defined criteria](#loosely-defined-criteria)
    - [Diagnosis of Hypertension](#diagnosis-of-hypertension)
    - [Prescription of Anti-Hypertensive medications](#prescription-of-anti-hypertensive-medications)
    - [Merging](#merging)
- [Exactly defined criteria](#exactly-defined-criteria)
    - [Inferred diagnosis of Hypertension](#inferred-diagnosis-of-hypertension)
    - [Instance 2](#instance-2)
- [Final Cohort](#final-cohort)

## ETL procedure

This cohort retrieved data from the Hospital Information System, Ramathibodi Hospital from January 2010 to June 2023. The goal was to streamline the cohort identification process while ensuring the minimal transfer of patient data to the data warehouse team, (*hereafter referred to as the DW team*). This process was executed in collaboration with the Data Health for Analysis Section (*informally known as the BI Team*), at the Faculty of Medicine, Ramathibodi Hospital. The methodology comprised two distinct stages.

The initial stage was designed to cast a broader net. Each DW team submitted criteria that were specific yet loosely defined, relevant to their respective projects. The BI team then extracted data related to various transactions – such as clinic visits, drug prescriptions, laboratory tests, and billing – that met any of the specified criteria. Following this, each DW team conducted a cohort identification procedure on these transactions to select potential patients.

During the second stage, the lists of these potential patients were forwarded to the BI team for the extraction of their complete medical history at Ramathibodi Hospital. The cohort identification process was then repeated as necessary to finalize the selection of eligible patients for the cohort.

### Instance 1

For instance, one of the criteria for Hypertension (HT) in this cohort is –

> In instances where patients were prescribed beta blockers but did not have a formal diagnosis of Hypertension recorded, these cases were classified as **inferred diagnosis of Hypertension**, provided there was no medical history of Hyperthyroidism.

In this scenerio, the loosely defined criteria for the initial data extraction was the "prescription of beta blockers". Consequently, the BI team transferred all transaction data involving patients who had been billed for beta blockers.

For exactly defined criteria, the DW team subsequently refined the data. They forwarded a list of patients who had been prescribed beta blockers but lacked a Hypertension diagnosis. The BI team then extracted the complete medical history of these patients. In cases where there was no record of Hyperthyroidism, the patients were categorized under inferred diagnosis of Hypertension.

## Loosely defined criteria

### Diagnosis of Hypertension

The methodology for identifying potential hypertension cases also involved the use of diagnosis data obtained from both clinic and in-patient visits. For this purpose, we focused on transactions that were coded according to the International Classification of Disease, 10th Revision, Thailand Modification (ICD10-TM). Specifically, we selected transactions that included ICD-10 diagnostic codes and their subcodes related to hypertension (HT).

The following table illustrates the ICD-10-TM codes used to identify different categories of hypertension:

ICD-10-TM Code | Diagnosis Description
-|-
I10* | Essential (primary) hypertension	
I11* | Hypertensive heart disease	
I12* | Hypertensive renal disease	
I13* | Hypertensive heart and renal disease		
I15* | Secondary hypertension	

These codes allowed for a comprehensive identification of patients with various forms of hypertension. By using these specific codes, we were able to accurately categorize patients based on their diagnosed condition, ensuring a precise and thorough cohort identification process.

### Prescription of Anti-Hypertensive medications

Given the often secondary or underlying nature of hypertension (HT) in clinical presentations, relying solely on primary complaints or diagnoses in electronic health records (EHRs) may not be sufficient for comprehensive patient identification. Supporting this, a previous study highlighted the limitations of using a single EHR source for patient identification in their study (Morley et al., [2014](https://doi.org/10.1371/journal.pone.0110900)). To address potential under-coding issues and enhance the accuracy of our cohort identification, we also incorporated medication data, specifically focusing on prescriptions for antihypertensive medications.

At Ramathibodi Hospital, each trade product is assigned a unique intra-institutional identifier, known as the Rama Drug Code. This code facilitates linkage with the Anatomical Therapeutic Chemical Classification System and Thai Medicines Terminology. For our study, we used the generic drug class or ATC level 5 to categorize antihypertensive medications. These were further grouped into specific drug classes relevant to our cohort:

1. Angiotensin-converting Enzyme Inhibitor
2. Angiotensin Receptor Blocker
3. Calcium Channel Blocker
4. Alpha Agonist
5. Alpha Blocker
6. Alpha Beta Blocker
7. Beta Blocker
8. Diuretic
9. Hydralazine
10. Minoxidil
11. Neprolysin Inhibitor
12. Renin Inhibitor 

Consistent with the 2019 Thai Guidelines on the Treatment of Hypertension (Kunanon et al., [2021](https://doi.org/10.35755/jmedassocthai.2021.10.12199)) , which recommends duotherapy in antihypertensive treatment, the use of combination pills is common at Ramathibodi Hospital. These combination pills often include multiple classes of antihypertensive drugs. A detailed mapping table linking each trade product to the corresponding cohort-specific drug class is available [here](https://github.com/CEB-Data-Science-Clinical-Informatics/Hypertension-Cohort/blob/main/maplist/cohort_anti_HT.xlsx). Please note that the Rama Drug Code is not disclosed in the mapping table.

### Merging

Utilizing these dual inclusion criteria – diagnosis codes and medication prescriptions – allowed us to categorize patients into three distinct groups based on their hypertension status:

1. Patients diagnosed with ICD-10 codes for hypertension and also prescribed antihypertensive medications.
2. Patients diagnosed with ICD-10 codes for hypertension but not prescribed any antihypertensive medications.
3. Patients prescribed antihypertensive medications but not diagnosed with hypertension according to ICD-10 codes.

For the purpose of this study, the first and second groups were considered to have a confirmed diagnosis of Hypertension. In determining the date of diagnosis for these patients, we used the earliest date at which they met either of the criteria, in cases where multiple dates were available. This approach ensured that we captured the onset of the disease as accurately as possible, which is crucial for understanding the progression and management of Hypertension.

The third group, consisting of patients prescribed antihypertensive medications without an ICD-10 diagnosis, presented a more complex scenario. These patients were to undergo further identification process for 'inferred diagnosis of Hypertension' as discussed earlier, recognizing the potential for under-diagnosis or under documentation in their medical records.

## Exactly defined criteria

### Inferred diagnosis of Hypertension

For the group of patients who were prescribed antihypertensive medications but did not have a corresponding ICD-10 diagnosis for hypertension, it was necessary to exercise caution before assuming a hypertension diagnosis. This is because antihypertensive medications, such as beta-blockers, are often prescribed for conditions other than hypertension, like hyperthyroidism. To accurately identify such cases, we consulted with internal medicine specialists to determine other common indications for which antihypertensive medications are prescribed.

Patients who were prescribed a single antihypertensive medication, like a beta-blocker, alongside a diagnosis for a corresponding condition, such as hyperthyroidism, were excluded from the hypertension cohort. This exclusion was based on the likelihood that the medication was prescribed for the non-hypertensive condition. To ensure accurate categorization, additional medical histories were extracted for these patients. The table below illustrates the antihypertensive medications and their other common indications, along with the associated ICD-10 codes:

Anti-Hypertensive Medication | Other Indication | ICD-10 Codes
-|-|-
ACEI | Heart Failure | I50, I501, I509
Alpha Blocker | Benign Prostatic Hyperplasia | N40, N401, N403
Alpha II Agonist | Hypertension in pregnancy | O13, O139
Beta Blocker | Atrial Fibrillation | I48, I480, I482, I489
Beta Blocker | Hyperthyroidism |E059 
Calcium Channel Blocker (non-Dihydropyridine) | Arrhythmias | I471, I498, I499, R000, R002, R008
Neprilysin Inhibitor | Heart Failure | I50, I501, I509

Patients who did not meet these exclusion criteria were considered to have an "inferred diagnosis of Hypertension". For these patients, the date of the first prescription of antihypertensive medication was taken as the date of diagnosis. This approach helped to identify those who might be under-diagnosed or whose diagnosis of hypertension was not documented in their medical records.

### Instance 2

For instance, consider a patient who received a bill for beta blocker medication based on loosely defined criteria. To investigate this, their entire medical history at Ramathibodi Hospital was reviewed to determine if a diagnosis of hyperthyroidism had ever been made. In the absence of such a diagnosis, it was inferred diagnosis of Hypertension.

If a diagnosis of hyperthyroidism was found, the initial date of the beta blocker prescription was compared with the date of the hyperthyroidism diagnosis. If the beta blocker prescription preceded the hyperthyroidism diagnosis, it was inferred diagnosis of hypertension. Conversely, if hyperthyroidism was diagnosed before the beta blocker prescription, the medication was deemed related to the thyroid condition rather than hypertension. It is important to note that in these analyses, the time gap between the diagnosis and the prescription was disregarded.

As mentioned, dual therapy regimens involving multiple drug classes are common. For example, in a hypothetical scenario where a patient was prescribed both an ACEI and an alpha blocker, their medical history was examined for indications of heart failure and benign prostatic hyperplasia. If either component of the combination therapy lacked a corresponding other indication, it was inferred diagnosis of Hypertension.

This approach can be summarized as:

> In instances where patients were prescribed anti-hypertensive medication but did not have a formal diagnosis of Hypertension recorded, these cases were classified as **inferred diagnosis of Hypertension**, provided at least one medication lacked a corresponding "other indication".

## Final cohort

All patients, including both diagnoses and inferred diagnoses, were included in the CEB Data Warehouse – Hypertension Theme. The data flow for cohort identification is described in the figure.

![13.5 years Data Warehouse for Hypertension January 2010 to June 2023](https://github.com/CEB-Data-Science-Clinical-Informatics/Hypertension-Cohort/blob/main/images/dataflow/2010_202306.png)