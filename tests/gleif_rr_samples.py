
"""Synthetic GLEIF RR-CDF 1.1 XML for tests. No real GLEIF data."""

RR_NS = "http://www.gleif.org/data/schema/rr/2016"


def synthetic_lei(prefix):
    base = (prefix + "0" * 18)[:18]
    check = 98 - int("".join(str(int(c, 36)) for c in base + "00")) % 97
    return f"{base}{check:02d}"


LOU = synthetic_lei("SYNTHLOU")


def relationship(child, parent, kind="IS_DIRECTLY_CONSOLIDATED_BY", status="ACTIVE",
                 registration="PUBLISHED", validation="ENTITY_SUPPLIED_ONLY",
                 documents="OTHER_OFFICIAL_DOCUMENTS", reference=None,
                 periods=(("ACCOUNTING_PERIOD", "2023-01-01T00:00:00Z", "2023-12-31T00:00:00Z"),
                          ("RELATIONSHIP_PERIOD", "2019-05-01T00:00:00Z", None)),
                 qualifiers=(("ACCOUNTING_STANDARD", "IFRS"),),
                 quantifiers=(("ACCOUNTING_CONSOLIDATION", "100", "PERCENTAGE"),),
                 child_type="LEI", last_update="2026-01-15T10:00:00Z", extra=""):
    def opt(tag, value):
        return f"<rr:{tag}>{value}</rr:{tag}>" if value is not None else ""

    period_xml = "".join(
        "<rr:RelationshipPeriod>" + opt("StartDate", s) + opt("EndDate", e)
        + f"<rr:PeriodType>{t}</rr:PeriodType></rr:RelationshipPeriod>" for t, s, e in periods)
    qualifier_xml = "".join(
        f"<rr:RelationshipQualifier><rr:QualifierDimension>{d}</rr:QualifierDimension>"
        f"<rr:QualifierCategory>{c}</rr:QualifierCategory></rr:RelationshipQualifier>"
        for d, c in qualifiers)
    quantifier_xml = "".join(
        f"<rr:RelationshipQuantifier><rr:MeasurementMethod>{m}</rr:MeasurementMethod>"
        f"<rr:QuantifierAmount>{a}</rr:QuantifierAmount>" + opt("QuantifierUnits", u)
        + "</rr:RelationshipQuantifier>" for m, a, u in quantifiers)
    return (
        "<rr:RelationshipRecord>\n  <rr:Relationship>"
        f"<rr:StartNode><rr:NodeID>{child}</rr:NodeID><rr:NodeIDType>{child_type}</rr:NodeIDType></rr:StartNode>"
        f"<rr:EndNode><rr:NodeID>{parent}</rr:NodeID><rr:NodeIDType>LEI</rr:NodeIDType></rr:EndNode>"
        f"<rr:RelationshipType>{kind}</rr:RelationshipType>"
        + (f"<rr:RelationshipPeriods>{period_xml}</rr:RelationshipPeriods>" if periods else "")
        + f"<rr:RelationshipStatus>{status}</rr:RelationshipStatus>"
        + (f"<rr:RelationshipQualifiers>{qualifier_xml}</rr:RelationshipQualifiers>" if qualifiers else "")
        + (f"<rr:RelationshipQuantifiers>{quantifier_xml}</rr:RelationshipQuantifiers>" if quantifiers else "")
        + "</rr:Relationship>\n  <rr:Registration>"
        "<rr:InitialRegistrationDate>2018-03-01T08:00:00Z</rr:InitialRegistrationDate>"
        f"<rr:LastUpdateDate>{last_update}</rr:LastUpdateDate>"
        f"<rr:RegistrationStatus>{registration}</rr:RegistrationStatus>"
        "<rr:NextRenewalDate>2027-03-01T08:00:00Z</rr:NextRenewalDate>"
        f"<rr:ManagingLOU>{LOU}</rr:ManagingLOU>"
        + opt("ValidationSources", validation) + opt("ValidationDocuments", documents)
        + opt("ValidationReference", reference)
        + f"</rr:Registration>{extra}\n</rr:RelationshipRecord>\n")


def document(records, content_date="2026-10-22T09:00:01.250Z", file_content="GLEIF_FULL_PUBLISHED",
             record_count=None, namespace=RR_NS, delta_start=None, prolog=""):
    count = len(records) if record_count is None else record_count
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>\n{prolog}'
        f'<rr:RelationshipData xmlns:rr="{namespace}">\n'
        f"<rr:Header><rr:ContentDate>{content_date}</rr:ContentDate>"
        f"<rr:FileContent>{file_content}</rr:FileContent>"
        + (f"<rr:DeltaStart>{delta_start}</rr:DeltaStart>" if delta_start else "")
        + f"<rr:RecordCount>{count}</rr:RecordCount></rr:Header>\n"
        "<rr:RelationshipRecords>\n" + "".join(records)
        + "</rr:RelationshipRecords>\n</rr:RelationshipData>\n")


def write(path, records, **header):
    path.write_text(document(records, **header), encoding="utf-8")
    return path
