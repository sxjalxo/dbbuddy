"""Base interface for mapping plugins"""


class MappingPlugin:
    """Base class for column mapping plugins"""
    
    def classify(self, column_name: str) -> str:
        """
        Classify a column name into a semantic term.
        
        Args:
            column_name: The name of the column to classify
            
        Returns:
            A semantic term (e.g., "value", "quantity", "name", "date", "identifier", "status", "description", "unknown")
        """
        raise NotImplementedError("Subclasses must implement classify()")
