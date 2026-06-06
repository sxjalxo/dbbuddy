"""Default mapping plugin with hardcoded semantic rules"""
from dbbuddy.plugins.base import MappingPlugin


class Plugin(MappingPlugin):
    """Default mapping plugin using hardcoded semantic rules"""
    
    MAP = {
        "amt": "value", "amount": "value", "price": "value",
        "cost": "value", "total": "value", "revenue": "value",
        "qty": "quantity", "quantity": "quantity", "count": "quantity",
        "num": "quantity", "number": "quantity",
        "name": "name", "title": "name", "label": "name",
        "date": "date", "time": "date", "created_at": "date",
        "updated_at": "date", "timestamp": "date",
        "id": "identifier", "uuid": "identifier", "key": "identifier",
        "status": "status", "state": "status", "flag": "status",
        "desc": "description", "description": "description",
        "note": "description", "comment": "description",
    }
    
    def classify(self, column_name: str) -> str:
        """
        Classify a column name using hardcoded semantic rules.
        
        Args:
            column_name: The name of the column to classify
            
        Returns:
            A semantic term (value, quantity, name, date, identifier, status, description, unknown)
        """
        col = column_name.lower()
        
        # Exact match
        if col in self.MAP:
            return self.MAP[col]
        
        # Substring match (longest keyword wins)
        matches = [(key, self.MAP[key]) for key in sorted(self.MAP, key=len, reverse=True) if key in col]
        if matches:
            return matches[0][1]
        
        return "unknown"
