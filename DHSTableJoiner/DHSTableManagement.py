#-------------------------------------------------------------------------------
# Name:        DHSTableManagement
# Purpose:     Provides methods for joining DHS tables to generate different
#              "recodes" or outputs.
#              Inputs must be separate tables e.g. REC01, RECH1, etc.
#
# Author:      Harry Gibson
#
# Created:     09/11/2015
# Copyright:   (c) Harry Gibson 2015
# Licence:     CC BY-SA 4.0
#-------------------------------------------------------------------------------

from itertools import chain
import warnings

def uniqList(input):
    """Utility func to remove duplicate elements from a list while preserving order (unlike a set)"""
    output = []
    for x in input:
        if x not in output:
            output.append(x)
    return output

class ColumnInfo:
    """Holds basic info defining a column. TODO add (and use) data type.
    
    This class currently doesn't really do anything that a 2-item (name, length) dictionary 
    as required for instantiating it wouldn't itself do, except be hashable so we can make a set.
    Equality / hashing is based on the name, and the length is ignored."""
    def __init__(self, NameLengthDictPair):
        self.Name = NameLengthDictPair["Name"]
        self.Length = int(NameLengthDictPair["Length"])
    def __str__(self):
        return self.Name
    def __eq__(self, other):
        return self.Name == other.Name
    def __ne__(self, other):
        return not self.__eq__(other)
    def __hash__(self):
        return hash(self.Name)
        
class TableInfo:
    """Represents information about a DB table and provides SQL to create it and update it"""
    def __init__(self, InputTableName, JoinColumns, OutputColumns):
        self._Name = InputTableName

        # do not sort join columns as we rely on the given order to join to other tables
        #j = [ColumnInfo(p) for p in JoinColumns]
        #o = [ColumnInfo(p) for p in OutputColumns]
        j = JoinColumns
        o = OutputColumns
        self._JoinColumns = uniqList(j)
        
        # create s as no-duplicates for table creation
        _uniqOut = sorted(set(o), key=lambda colinfo: colinfo.Name)
        _outNoJoin = [c for c in _uniqOut if c not in j]
        _joinToOut = [c for c in j if c in _uniqOut]

        # output columns is whatever was provided but checked for duplicates and sorted,
        # may or may not contain join cols and if it does they are at the start
        # and not sorted
        self._OutputColumns = list(chain(_joinToOut, _outNoJoin))

        # all columns always contains the join columns in the order they were given
        # and then the other output columns in sorted order, without duplicates
        self._allColumns = list(chain(self._JoinColumns, _outNoJoin))

    def Name(self):
        """Get the table name, string"""
        return self._Name

    def JoinColumns(self):
        """Get the list(string) of join column names"""
        return [str(c) for c in self._JoinColumns]
    def JoinColumnsDetails(self):
        """Get the list(ColumnInfo) of join column details"""
        return self._JoinColumns

    def OutputColumns(self, asString=False, qualified=False):
        """Get the columns (list or string) that should be used for output

        It is alphabetically sorted and any duplicates removed, but otherwise is the
        same as the OutputColumns provided at construction.
        This may or may not include the join columns. """
        if qualified:
            tmp = [self._Name + "." + str(f)
                   for f in self._OutputColumns]
        else:
            tmp = [str(c) for c in self._OutputColumns]
        if asString:
            return ", ".join(tmp)
        else:
            return tmp
            
    def OutputColumnsDetails(self):
        """Get the list(ColumnInfo) of output column details"""
        return self._OutputColumns

    def AllColumns(self):
        """Get the list(string) of all columns of this table, including join columns.

        Join columns will be specified first in their original order then other columns
        in alphabetical order.
        """
        return [str(c) for c in self._allColumns]
    
    def AllColumnsDetails(self):
        """Get the list(ColumnInfo) of all columns of this table, including join columns."""
        return self._allColumns

    def GetCreateTableSQL(self):
        """Get the SQL necessary to create this table in the DB. All columns are type text.
        
        TODO - add data type to columninfo object and use it here to create columns of numeric etc 
        type when possible"""
        fmt = "CREATE TABLE {0} ({1});"
        return fmt.format(self._Name,
                          ", ".join([t + " text" for t in self.AllColumns()]))

    def GetInsertSQLTemplate(self):
        """Get the parameterized SQL to insert rows in the DB"""
        fmt = "INSERT INTO {0} ({1}) VALUES ({2})"
        return fmt.format(self._Name,
                          ", ".join(self.AllColumns()),
                          ",".join(["?" for i in self.AllColumns()]))

    def GetCreateIndexSQL(self):
        """Get the SQL necessary to create indexes on all this table's join columns
        
        If more than 1 join column exists, also creates a covering index on all of them."""
        fmt = "CREATE INDEX {0} ON {1}({2});"
        idxName = "{0}_{1}"
        stmts = [fmt.format(idxName.format(jc, self._Name),
                            self._Name, jc) for jc in self.JoinColumns()]
        if len(self.JoinColumns()) > 0:
            allStmt = fmt.format(idxName.format("ALLIDX",self._Name),
                                 self._Name, ",".join(self.JoinColumns()))
            stmts.append(allStmt)
        return "\n".join(stmts)

class TableToTableFieldCopier:
    """Provides functionality to copy fields from an input to an output table

    It provides the necessary SQL, as a string, to transfer data in various different ways.
    Actually executing the SQL in a database is up to the caller.

    Both tables must already exist in the DB and be represented as corresponding TableInfo objects.
    Only the fields specified in TransferFields (and which actually exist in
    InputTable) will be copied."""
    def __init__(self, OutputTable, InputTable, TransferFields):
        self._OutputTable = OutputTable
        self._InputTable = InputTable
        self._TransferFields = [f for f in TransferFields
                        if f in InputTable.AllColumns()]

    #def _GetTransferReferences(self, tablealias):
    #    return ", ".join([tablealias + "." + f for f in self._TransferFields])

    def _GetJoinExpr(self, outCol, inCol):
        """ Returns the expression part of a join clause for one pair of table/columns

        Allows for the special case of CASEID joining to a none-caseid column
        for use with DHS or other CSPro format data, e.g. to produce a M:1 join between
        a record for an individual (woman) and the parent record (household).
        """
        fmtSubStr = "substr({0}.{1}, 1, length({0}.{1})-{2})"
        fmt = "{0} = {1}"
        expr = "{0}.{1}"
        # The CASEID variable embeds 2 ids within it. With the last three
        # characters removed (some of which may be spaces) it becomes the HHID.
        # In hindsight it would have been better to store that separately when
        # processing the raw DHS data!
        # So to join a table with CASEID to a table with HHID we need to modify
        # CASEID.
        # We will assume that any time we are joining two vars of differing lengths,
        # this is the reason why and we should trim the longer to match the shorter.
        # (We could also use a query like
        # SELECT * from REC21 LEFT JOIN RECH0 ON REC21.CASEID LIKE RECH0.HHID||'%'
        # but that's very slow)
        outTable = self._OutputTable.Name()
        inTable = self._InputTable.Name()
        if outCol.Length and inCol.Length and inCol.Length < outCol.Length:
            lenDiff = outCol.Length - inCol.Length
            leftExpr = fmtSubStr.format(outTable, outCol, str(lenDiff))
        #if outCol == "CASEID" and inCol == "HHID" :
        #    leftExpr = fmtSubStr.format(outTable, outCol)
        else:
            leftExpr = expr.format(outTable, outCol)
        #if outCol == "HHID" and inCol == "CASEID":
        #    rightExpr = fmtSubStr.format(inTable, inCol)
        if outCol.Length and inCol.Length and inCol.Length > outCol.Length:
            lenDiff = inCol.Length - outCol.Length
            rightExpr = fmtSubStr.format(inTable, inCol, str(lenDiff))
            # This suggests we are joining HHID (left) to CASEID (right) which would be 1:M
            strWarn = ("Right hand table ({0}) has a longer id column than ".format(inTable)+
                "left hand table ({0}). Are you accidentally creating a 1:M join?".format(outTable))
            warnings.warn(strWarn)
        else:
            rightExpr = expr.format(inTable, inCol)
        return fmt.format(leftExpr, rightExpr)

    def _GetJoinClause(self):
        """ Return the JOIN ON clause for joining the output to the input table.

        For example
        'outputTable.IDX1 = inputTable.IDNum AND outputTable.IDX2 = inputTable.Ref'
        The order of the join columns as provided at construction of the TableInfo
        objects is used to infer which column should be joined to which.
        """
        myJoinCols = self._OutputTable.JoinColumnsDetails()
        otherJoinCols = self._InputTable.JoinColumnsDetails()
        # If we are joining to a table with fewer join fields then only use
        # the common number of fields
        # For example the output table may specify CASEID and BIDX and can
        # join to a child table (1:1) on both of these, but can also join
        # to a parent table (M:1) on only CASEID
        # *** we assume the columns are in the matching order! ***
        if len(myJoinCols) < len(otherJoinCols):
            warnings.warn(
                "Right table {0} has more join columns than left table {1} - ".format(
                self._InputTable.Name(), self._OutputTable.Name())+
                "Are you accidentally creating a 1:M join?"
                )
            otherJoinCols = otherJoinCols[0:len(myJoinCols)]
        elif len(myJoinCols) > len(otherJoinCols):
            myJoinCols = myJoinCols[0:len(otherJoinCols)]

        joinSQL = " and ".join([self._GetJoinExpr(
                                    myJoinCols[i],
                                    otherJoinCols[i])
            for i in range (len(myJoinCols))
            ]
        )
        return joinSQL

    def _GetTransferClause(self):
        """ Returns the SET clause for copying data from input to output table.

        For example
        'outputTable.datacol1 = inputTable.datacol1'
        """
        # we don't support recording different input/output col names
        basicPart = "{0}.{1} = {2}.{1}"
        transferCols = self._TransferFields
        parts = [basicPart.format(self._OutputTable.Name(),
                         transferCols[i],
                         self._InputTable.Name(),
                         transferCols[i])
            for i in range(len(transferCols))
        ]
        return ", ".join(parts)

    def GetTransferFields(self, asString=True, qualified=False):
        """Returns the list of fields to be copied from input to output table.

        Output is either as a list or as a comma delimited string. Fields may be
        qualified with the table name, or not."""
        #GetTransferReferences(self._InputTable.Name()
        if qualified:
            tmp = [self._InputTable.Name() + "." + f
                   for f in self._TransferFields]
        else:
            tmp = self._TransferFields
        if asString:
            return ", ".join(tmp)
        else:
            return tmp

    def GetUpdateSQL_Join(self):
        """ Returns the SQL to transfer the required fields from the input to the output table.

        This version uses an inner join on the update query. This is efficient but
        SQLite doesn't support it so it is no use with SQLite db. (I found that out
        after writing it!). It may be useful in other DBs.

        For example
        UPDATE {outTbl} INNER JOIN {inTbl} ON {inTbl.id1 = outTbl.id1 AND ... }
        SET {outTblCol1 = inTblCol1, outTblCol2 = inTblCol2...};
        """
        sqlJoinTemplate = "UPDATE {0} INNER JOIN {1} ON {2} SET {3};"

        # UPDATE {0} o INNER JOIN {1} i on o.{2} = i.{3} SET o.{4} = i.{5};
        joinClause = self._GetJoinClause()
        transferClause = self._GetTransferClause()
        fields = self._GetTransferFields()
        refs = self._GetTransferReferences(self._InputTable.Name())

        outSQL = sqlJoinTemplate.format(self._OutputTable.Name(),
                           self._InputTable.Name(),
                           joinClause,
                           transferClause
                           )
        return outSQL


    def GetUpdateSQL_Replace(self):
        """ Returns the SQL to transfer the required fields from the input to the output table.

        This version uses REPLACE INTO syntax so only works for the first one on the output,
        subsequent uses will cause duplicate rows.

        For example
        'REPLACE INTO output(H0, H1, H2) SELECT i.H0, i.H1, i.H2
        FROM output o INNER JOIN REC43 i ON o.CASEID = i.CASEID;'
        """
        sqlReplaceTemplate = "REPLACE INTO {0}({1}) SELECT {2} FROM {0} INNER JOIN {3} ON {4};"

        joinClause = self._GetJoinClause()
        #transferClause = self._GetTransferClause()
        fields = self._GetTransferFields()
        #refs = self._GetTransferReferences(self._InputTable.Name())
        refs = self._GetTransferFields(qualified=True)
        outSQL = sqlReplaceTemplate.format(self._OutputTable.Name(),
                           fields,
                           refs,
                           self._InputTable.Name(),
                           joinClause
                           )
        return outSQL

    def _GetSubQuery(self, colname):
        fmt = "(SELECT {0} FROM {1} WHERE {2})"
        sql =  fmt.format(colname,
                          self._InputTable.Name(),
                          self._GetJoinClause()
        )
        return sql

    def GetUpdateSQL_SQLite(self):
        """
        Generates SQL for a join-update query that works with SQLite, using subqueries.

        All id (join) columns should be indexed else this is likely to be very slow.
        For example
        UPDATE outputTbl SET
            outCol1 = (SELECT outCol1
                      FROM inputTbl
                      WHERE idCol = outputTbl.idCol)
            , outCol2 = (SELECT outCol2
                      FROM inputTbl
                      WHERE idCol = outputTbl.idCol);
        """
        fmt = "UPDATE {0} SET {1}"
        fields = self._TransferFields
        setClause = ", ".join([col + " = " + self._GetSubQuery(col) for col in fields])
        sql = fmt.format(self._OutputTable.Name(), setClause)
        return sql


class TableToTableTransferrer(TableToTableFieldCopier):
    """ Extends TableToTableFieldCopier to also provide a method for copying rows directly.

    Used for initial population of an output table with values from a "master" table, so
    that the seeded table can then be joined to other tables.

    For example
    INSERT INTO {outTbl}({col1, col2...}) SELECT {col1, col2...} FROM {inTbl}
    """
    transferSQLTemplate = 'INSERT INTO  {0}({1}) SELECT {1} FROM {2}'

    def __init__(self, OutputTable, InputTable):
        self._OutputTable = OutputTable
        self._InputTable = InputTable
        infields = self._InputTable.AllColumns()
        self._TransferFields = infields

    def GetTransferSQL(self):

        fields = ", ".join(self._TransferFields)
        return self.transferSQLTemplate.format(self._OutputTable.Name(),
                                          fields,
                                          self._InputTable.Name()
                                          )


class MultiTableJoiner():
    """Creates an output table by joining multiple input tables together.

    The input tables are to be provided as a list of TableInfo objects.
    The first one in the list is taken as the "master" table - all rows from
    this table will be included and the other tables will all be LEFT OUTER
    joined to it. So relative to this master table all other tables must join
    either 1:1 or M:1.

    Currently all defined output columns from all the input
    tables will be used.
    
    TODO - add ability to extract coded value labels c.f. ESRI coded value domains.
    See http://desktop.arcgis.com/en/arcmap/10.3/manage-data/using-sql-with-gdbs/example-resolving-domain-codes-to-description-values.htm
    
    """
    def __init__(self, OutputTableName, InputTablesList):
        self._OutputTableName = OutputTableName
        self._MasterTable = InputTablesList[0]
        self._InputTableList = InputTablesList[1:]
        self._TableCopiers = [TableToTableFieldCopier(
                                self._MasterTable, it, it.OutputColumns())
         for it in self._InputTableList
        ]

    #def _GetOuterJoinClause(self, leftTable, rightTable):
    #    fmt = "LEFT JOIN {0} ON {1}"
    #    return fmt.format(leftTable, self._GetInnerJoinClause(leftTable, rightTable))

    def GetCreateIntoSQL(self, QualifyFieldNames=False):
        """Get the SQL required to create the output table from the joined inputs.

        For example:
        CREATE TABLE MyOutputTableName AS 
        SELECT REC21.CASEID, REC21.COLX, REC43.COLY, REC41.COLZ from REC21
           LEFT JOIN REC43 ON REC21.CASEID = REC43.CASEID and REC21.BIDX = REC43.HIDX
           LEFT JOIN REC41 ON REC21.CASEID = REC41.CASEID and REC21.BIDX = REC41.MIDX
           ....
        This is the main function from this module which would normally be called by 
        client code.
        """
        tblJoinTemplate = "LEFT JOIN {0} ON {1}"
        selectTemplate = "SELECT \n{0} FROM \n {1} \n{2}"
        outputTemplate = "CREATE TABLE {0} AS {1}"
        # create the left-join clause for the select statement, to
        # include all the tables we are joining
        tblJoins = " \n".join([tblJoinTemplate.format(
                        i._InputTable.Name(), i._GetJoinClause())
                    for i in self._TableCopiers])
        # build the list of columns to select out of the join - it's
        # all the columns of all the joined tables, but they need to be
        # qualified where the same name is in multiple tables
        tmp = self._MasterTable.OutputColumns(qualified=True, asString=False)
        foreign = [i.GetTransferFields(qualified=True, asString=False)
                   for i in self._TableCopiers]
        flatforeign = chain.from_iterable(foreign)
        tmp.extend(flatforeign)
        tblFields = tmp#self._MasterTable.OutputColumns().extend
        #                                ([i._InputTable.OutputColumns() for i in self._TableCopiers]))
        print (len(tblFields))
        tblFieldsUniq = uniqList(tblFields)
        if QualifyFieldNames:
            qualFlds = [i + " as "+i.replace(".","_") for i in tblFieldsUniq]
            tblFieldsUniq = qualFlds
        uniqFlds = ", ".join(tblFieldsUniq)#chain()
        print (len(tblFieldsUniq))
        sel = selectTemplate.format(uniqFlds, self._MasterTable.Name(), tblJoins)
        outSQL = outputTemplate.format(self._OutputTableName, sel)
        return outSQL


