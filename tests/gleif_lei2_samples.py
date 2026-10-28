
"""Synthetic GLEIF LEI-CDF 3.1 XML for tests. No real GLEIF data."""

from gleif_rr_samples import LOU, synthetic_lei

LEI_NS = "http://www.gleif.org/data/schema/leidata/2016"
HEADER_NS = "http://www.gleif.org/concatenated-file/header-extension/2.0"


def address(tag="LegalAddress", lang="de", line="Synthetic Street 1", city="Synthville",
            country="CH", extra_lines=(), region=None, postal="1000", type_=None):
    attrs = (f' xml:lang="{lang}"' if lang else "") + (f' type="{type_}"' if type_ else "")
    return (f"<lei:{tag}{attrs}><lei:FirstAddressLine>{line}</lei:FirstAddressLine>"
            + "".join(f"<lei:AdditionalAddressLine>{x}</lei:AdditionalAddressLine>" for x in extra_lines)
            + f"<lei:City>{city}</lei:City>"
            + (f"<lei:Region>{region}</lei:Region>" if region else "")
            + f"<lei:Country>{country}</lei:Country>"
            + (f"<lei:PostalCode>{postal}</lei:PostalCode>" if postal else "") + f"</lei:{tag}>")


def event(kind="CHANGE_LEGAL_NAME", status="COMPLETED", group="STANDALONE",
          effective="2022-01-01T00:00:00+01:00", recorded="2022-02-01T10:00:00.000+01:00",
          documents="SUPPORTING_DOCUMENTS", reference=None, affected=(), extra_attrs=""):
    return (f'<lei:LegalEntityEvent group_type="{group}" event_status="{status}"{extra_attrs}>'
            f"<lei:LegalEntityEventType>{kind}</lei:LegalEntityEventType>"
            + (f"<lei:LegalEntityEventEffectiveDate>{effective}</lei:LegalEntityEventEffectiveDate>"
               if effective else "")
            + f"<lei:LegalEntityEventRecordedDate>{recorded}</lei:LegalEntityEventRecordedDate>"
            f"<lei:ValidationDocuments>{documents}</lei:ValidationDocuments>"
            + (f"<lei:ValidationReference>{reference}</lei:ValidationReference>" if reference else "")
            + ("<lei:AffectedFields>" + "".join(
                f'<lei:AffectedField field_xpath="{x}">{v}</lei:AffectedField>' for x, v in affected)
               + "</lei:AffectedFields>" if affected else "")
            + "</lei:LegalEntityEvent>")


def record(lei=None, name="Synthetic Holdings AG", name_lang="de", other_names=(),
           transliterated=(), status="ACTIVE", category="GENERAL", sub_category=None,
           jurisdiction="CH", legal_form=("MVII", None), successors=(), events=(),
           creation="2001-05-04T00:00:00+02:00", registration_status="ISSUED",
           validation="FULLY_CORROBORATED", authority=True, validation_authority=True,
           entity_extra="", registration_extra="", record_extra="", headquarters=None):
    lei = lei or synthetic_lei("SYNTHLEIA")
    others = ("<lei:OtherEntityNames>" + "".join(
        f'<lei:OtherEntityName xml:lang="{lang}" type="{kind}">{text}</lei:OtherEntityName>'
        for text, kind, lang in other_names) + "</lei:OtherEntityNames>") if other_names else ""
    translits = ("<lei:TransliteratedOtherEntityNames>" + "".join(
        f'<lei:TransliteratedOtherEntityName xml:lang="{lang}" type="{kind}">{text}'
        "</lei:TransliteratedOtherEntityName>" for text, kind, lang in transliterated)
        + "</lei:TransliteratedOtherEntityNames>") if transliterated else ""
    successor_xml = "".join(
        f"<lei:SuccessorEntity><lei:SuccessorLEI>{s}</lei:SuccessorLEI></lei:SuccessorEntity>"
        if not s.startswith("name:") else
        f'<lei:SuccessorEntity><lei:SuccessorEntityName xml:lang="de">{s[5:]}'
        "</lei:SuccessorEntityName></lei:SuccessorEntity>" for s in successors)
    return (
        f"<lei:LEIRecord><lei:LEI>{lei}</lei:LEI><lei:Entity>"
        f'<lei:LegalName xml:lang="{name_lang}">{name}</lei:LegalName>{others}{translits}'
        + address("LegalAddress") + (headquarters or address("HeadquartersAddress"))
        + ("<lei:RegistrationAuthority><lei:RegistrationAuthorityID>RA000548"
           "</lei:RegistrationAuthorityID><lei:RegistrationAuthorityEntityID>CHE-000.000.001"
           "</lei:RegistrationAuthorityEntityID></lei:RegistrationAuthority>" if authority else "")
        + (f"<lei:LegalJurisdiction>{jurisdiction}</lei:LegalJurisdiction>" if jurisdiction else "")
        + (f"<lei:EntityCategory>{category}</lei:EntityCategory>" if category else "")
        + (f"<lei:EntitySubCategory>{sub_category}</lei:EntitySubCategory>" if sub_category else "")
        + (f"<lei:LegalForm><lei:EntityLegalFormCode>{legal_form[0]}</lei:EntityLegalFormCode>"
           + (f"<lei:OtherLegalForm>{legal_form[1]}</lei:OtherLegalForm>" if legal_form[1] else "")
           + "</lei:LegalForm>" if legal_form else "")
        + f"<lei:EntityStatus>{status}</lei:EntityStatus>"
        + (f"<lei:EntityCreationDate>{creation}</lei:EntityCreationDate>" if creation else "")
        + successor_xml
        + ("<lei:LegalEntityEvents>" + "".join(events) + "</lei:LegalEntityEvents>" if events else "")
        + entity_extra + "</lei:Entity><lei:Registration>"
        "<lei:InitialRegistrationDate>2014-01-01T00:00:00Z</lei:InitialRegistrationDate>"
        "<lei:LastUpdateDate>2026-01-01T09:30:00.000+01:00</lei:LastUpdateDate>"
        f"<lei:RegistrationStatus>{registration_status}</lei:RegistrationStatus>"
        "<lei:NextRenewalDate>2027-01-01T00:00:00Z</lei:NextRenewalDate>"
        f"<lei:ManagingLOU>{LOU}</lei:ManagingLOU>"
        + (f"<lei:ValidationSources>{validation}</lei:ValidationSources>" if validation else "")
        + ("<lei:ValidationAuthority><lei:ValidationAuthorityID>RA000548</lei:ValidationAuthorityID>"
           "</lei:ValidationAuthority>" if validation_authority else "")
        + registration_extra + f"</lei:Registration>{record_extra}</lei:LEIRecord>\n")


def document(records, content_date="2026-10-22T09:00:01Z", file_content="GLEIF_FULL_PUBLISHED",
             record_count=None, namespace=LEI_NS, sources=None, prolog="", records_extra=""):
    count = len(records) if record_count is None else record_count
    extension = ""
    if sources is not None:
        extension = (f'<lei:Extension><gleif:Sources xmlns:gleif="{HEADER_NS}">' + "".join(
            f"<gleif:Source><gleif:ContentDate>{d}</gleif:ContentDate><gleif:Originator>{o}"
            f"</gleif:Originator><gleif:RecordCount>{n}</gleif:RecordCount></gleif:Source>"
            for d, o, n in sources) + "</gleif:Sources></lei:Extension>")
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{prolog}'
            f'<lei:LEIData xmlns:lei="{namespace}">\n<lei:LEIHeader>'
            f"<lei:ContentDate>{content_date}</lei:ContentDate>"
            f"<lei:FileContent>{file_content}</lei:FileContent>"
            f"<lei:RecordCount>{count}</lei:RecordCount>{extension}</lei:LEIHeader>\n"
            "<lei:LEIRecords>\n" + "".join(records) + records_extra
            + "</lei:LEIRecords>\n</lei:LEIData>\n")


def write(path, records, **kwargs):
    path.write_text(document(records, **kwargs), encoding="utf-8")
    return path
